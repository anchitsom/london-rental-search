"""
EPC certificate vision enrichment via Ollama + qwen2.5vl:3b.

Public function: extract_epc_from_image(image_url: str, timeout: float = 60.0) -> dict

Mirrors homehunt/floorplan_vision.py for the Ollama call shape (same /api/chat
endpoint, same qwen2.5vl:3b model, same format=json and num_predict=128
request body). The vision model reads the highlighted SAP arrow letter
unreliably on small certificate images, so the load-bearing precision gate
is reconcile() (lifted verbatim from epc_enrichment/paths/sap_sanity.py),
which derives the rating letter from the SAP score using the canonical UK
bucket map. Internally inconsistent reads (e.g. potential_sap < current_sap)
are rejected with confidence="rejected"; reads that lack a current SAP score
are also rejected.

Returned dict fields:
    epc_rating          str  | None   one of A-G when reconciled, else None
    sap_score_current   int  | None   the SAP number the model read for current
    sap_score_potential int  | None   the SAP number the model read for potential
    confidence          str           "sap_derived" | "rejected" | "error"
    raw                 dict | None   the parsed model response, for audit
    error               str  | None   the underlying error text on transport / parse fail
"""

import asyncio
import base64
import json
import os
from typing import Optional

import httpx
from structlog import get_logger

logger = get_logger()

OLLAMA_BASE = "http://127.0.0.1:11434"
EPC_VISION_MODEL = "qwen2.5vl:3b"

# Bound concurrent EPC vision calls. Mirrors floorplan_vision.py; on Apple
# Silicon Metal a 3B vision model can crash GGML under heavy parallel load.
_EPC_VISION_SEM = asyncio.Semaphore(int(os.environ.get("EPC_VISION_MAX_CONCURRENT", "2")))

# ---------------------------------------------------------------------------
# SAP bucket sanity gate. Lifted verbatim from
# epc_enrichment/paths/sap_sanity.py per the Wave 1.7 plan.
# ---------------------------------------------------------------------------

_SAP_BUCKETS = {
    "A": (92, 100),
    "B": (81, 91),
    "C": (69, 80),
    "D": (55, 68),
    "E": (39, 54),
    "F": (21, 38),
    "G": (1, 20),
}

_LETTER_RANK = {l: i for i, l in enumerate("ABCDEFG")}


def letter_from_sap(sap: Optional[int]) -> Optional[str]:
    """Return the canonical EPC letter for a SAP score."""
    if sap is None:
        return None
    for letter, (lo, hi) in _SAP_BUCKETS.items():
        if lo <= sap <= hi:
            return letter
    return None


def reconcile(
    current_rating: Optional[str],
    current_sap: Optional[int],
    potential_rating: Optional[str] = None,
    potential_sap: Optional[int] = None,
) -> dict:
    """
    Given a vision read, return the most defensible (rating, confidence) pair.

    Strategy:
      - If the current_sap is in [1, 100] and consistent with the potential
        (potential_sap >= current_sap when both present), trust the SAP and
        derive the letter from it. The vision model reads numbers more
        reliably than coloured arrows on the SAP-rated certificate.
      - If the current_sap is missing or inconsistent with potential_sap,
        return None (rescue treated as a miss).

    The original current_rating letter is recorded for audit but not used
    to derive the canonical answer.

    Returns:
      {
        "reconciled_rating": str | None,
        "reconciled_sap": int | None,
        "confidence": "sap_derived" | "letter_only" | "miss",
        "reason": str,
      }
    """
    out = {
        "reconciled_rating": None,
        "reconciled_sap": None,
        "confidence": "miss",
        "reason": "",
    }
    if current_sap is None:
        out["reason"] = "missing_current_sap"
        return out
    if not (1 <= current_sap <= 100):
        out["reason"] = "current_sap_out_of_range"
        return out
    # Potential check: if both present and potential < current, the read is
    # internally inconsistent and we cannot trust either number.
    if potential_sap is not None and potential_sap < current_sap:
        out["reason"] = "potential_sap_below_current"
        return out
    derived = letter_from_sap(current_sap)
    if derived is None:
        out["reason"] = "sap_not_in_any_bucket"
        return out
    out["reconciled_rating"] = derived
    out["reconciled_sap"] = current_sap
    out["confidence"] = "sap_derived"
    out["reason"] = (
        "ok" if derived == current_rating
        else f"sap_derived_letter_overrides_model_letter({current_rating})"
    )
    return out


def is_consistent(
    current_rating: Optional[str],
    current_sap: Optional[int],
    potential_rating: Optional[str] = None,
    potential_sap: Optional[int] = None,
) -> bool:
    """
    True when the read is internally consistent. False otherwise.
    """
    if current_rating not in _SAP_BUCKETS:
        return False
    if current_sap is None:
        return False
    lo, hi = _SAP_BUCKETS[current_rating]
    if not (lo <= current_sap <= hi):
        return False
    if potential_rating and potential_rating in _SAP_BUCKETS:
        # Potential rating cannot be worse (higher letter index) than current
        if _LETTER_RANK[potential_rating] > _LETTER_RANK[current_rating]:
            return False
    if potential_sap is not None:
        if potential_sap < current_sap:
            return False
    return True


# ---------------------------------------------------------------------------
# Ollama call
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are an EPC (UK Energy Performance Certificate) data extractor. "
    "Respond ONLY with valid JSON. Use ASCII-only string values. Never embed "
    "raw quote characters inside a JSON string value."
)

_USER_PROMPT = """\
Analyse this UK EPC (Energy Performance Certificate) chart image and return
a single JSON object with exactly the following keys.

{
  "current_rating": "<single letter A B C D E F or G, the highlighted current rating>",
  "current_sap": <integer 1 to 100, the current SAP score on the chart>,
  "potential_rating": "<single letter A B C D E F or G, the potential rating>",
  "potential_sap": <integer 1 to 100, the potential SAP score after upgrades>
}

Rules:
- Read numbers off the chart left column for current and right column for potential.
- The current rating is the letter band the highlighted arrow points to.
- The potential rating is the letter band after recommended upgrades.
- If a value is illegible, use null for that field.
"""


def _empty_result(error: Optional[str], confidence: str = "error") -> dict:
    return {
        "epc_rating": None,
        "sap_score_current": None,
        "sap_score_potential": None,
        "confidence": confidence,
        "raw": None,
        "error": error,
    }


def _coerce_int(value) -> Optional[int]:
    """Best-effort int conversion; returns None on miss."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_letter(value) -> Optional[str]:
    """Return a single uppercase A-G letter or None."""
    if value is None:
        return None
    s = str(value).strip().upper()
    if len(s) >= 1 and s[0] in _SAP_BUCKETS:
        return s[0]
    return None


def _parse_model_response(raw_text: str) -> dict:
    """Parse the model's JSON response. Raises ValueError on parse failure."""
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return json.loads(text)


async def _fetch_image_b64(image_url: str, client: httpx.AsyncClient) -> str:
    """Fetch an image URL and return its base64-encoded content."""
    resp = await client.get(image_url)
    resp.raise_for_status()
    return base64.b64encode(resp.content).decode("ascii")


async def extract_epc_from_image(image_url: str, timeout: float = 60.0) -> dict:
    """
    Extract an EPC rating from a single certificate image URL.

    Calls Ollama qwen2.5vl:3b to read the SAP scores from the EPC chart
    image, then runs reconcile() to derive the canonical rating letter from
    the current SAP. The vision model reads numbers more reliably than the
    highlighted arrow letter, so the SAP-derived letter is the trusted
    answer.

    Confidence labels:
      "sap_derived" -- SAP read passed the reconciliation gate; rating is trusted
      "rejected"    -- vision parse succeeded but the read is inconsistent
      "error"       -- image fetch or vision parse failed

    Returns the dict described in the module docstring.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            image_b64 = await _fetch_image_b64(image_url, client)

            payload = {
                "model": EPC_VISION_MODEL,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": _USER_PROMPT,
                        "images": [image_b64],
                    },
                ],
                "stream": False,
                "format": "json",
                "options": {
                    "temperature": 0.0,
                    "num_predict": 128,
                    "num_ctx": 4096,
                },
            }

            async with _EPC_VISION_SEM:
                resp = await client.post(
                    f"{OLLAMA_BASE}/api/chat",
                    json=payload,
                )
                resp.raise_for_status()
                body = resp.json()

        raw_text = body.get("message", {}).get("content", "")
        if not raw_text:
            logger.warning("epc_vision_empty_response", url=image_url)
            return _empty_result("empty_response")

        parsed = _parse_model_response(raw_text)

        current_rating = _coerce_letter(parsed.get("current_rating"))
        current_sap = _coerce_int(parsed.get("current_sap"))
        potential_rating = _coerce_letter(parsed.get("potential_rating"))
        potential_sap = _coerce_int(parsed.get("potential_sap"))

        rec = reconcile(current_rating, current_sap, potential_rating, potential_sap)

        if rec["confidence"] == "sap_derived":
            logger.info(
                "epc_vision_sap_derived",
                url=image_url,
                model_letter=current_rating,
                sap=current_sap,
                derived=rec["reconciled_rating"],
                reason=rec["reason"],
            )
            return {
                "epc_rating": rec["reconciled_rating"],
                "sap_score_current": current_sap,
                "sap_score_potential": potential_sap,
                "confidence": "sap_derived",
                "raw": parsed,
                "error": None,
            }

        # Reconciliation rejected the read.
        logger.info(
            "epc_vision_rejected",
            url=image_url,
            model_letter=current_rating,
            sap=current_sap,
            potential_sap=potential_sap,
            reason=rec["reason"],
        )
        return {
            "epc_rating": None,
            "sap_score_current": current_sap,
            "sap_score_potential": potential_sap,
            "confidence": "rejected",
            "raw": parsed,
            "error": rec["reason"] or "rejected",
        }

    except json.JSONDecodeError as exc:
        logger.warning("epc_vision_json_parse_error", url=image_url, error=str(exc))
        return _empty_result(f"json_parse_error: {exc}")
    except ValueError as exc:
        logger.warning("epc_vision_value_error", url=image_url, error=str(exc))
        return _empty_result(f"value_error: {exc}")
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "epc_vision_http_error",
            url=image_url,
            status=exc.response.status_code,
        )
        return _empty_result(f"http_error_{exc.response.status_code}")
    except Exception as exc:
        logger.warning("epc_vision_call_failed", url=image_url, error=str(exc))
        return _empty_result(f"call_failed: {exc}")
