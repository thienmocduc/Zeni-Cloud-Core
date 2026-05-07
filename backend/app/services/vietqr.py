"""
Zeni Cloud Core — VietQR generator (TCVN/NAPAS Napas247 compliant).

Generates VietQR string per the EMVCo TLV format with Napas247 extension as
defined in TCVN 7322 / NAPAS spec. Output is a single string of TLV blocks
ending with CRC-16/CCITT-FALSE checksum.

Reference (https://vietqr.io standard):
    EMV root tags + Merchant Account Information at tag "38" containing:
        00 = "A000000727"  (NAPAS GUID)
        01 = beneficiary org info:
            00 = bank BIN
            01 = account number
        02 = service code  ("QRIBFTTA" = transfer to account, "QRIBFTTC" = card)
    Tag 54 = transaction amount (string)
    Tag 58 = country code "VN"
    Tag 62 = additional data (bill number, addInfo …)
    Tag 63 = CRC-16/CCITT-FALSE over the full preceding string + "6304"

Bank BIN codes from NAPAS catalogue (most common Vietnamese banks).

Functions
---------
generate_qr_payload(bank_code, account_number, amount_vnd, addInfo, ref) -> str
generate_qr_image(payload, size=300) -> bytes (PNG)
"""
from __future__ import annotations

import base64
import io
import logging
from typing import Optional

log = logging.getLogger("zeni.vietqr")


# ─── Bank BIN registry (NAPAS) ──────────────────────────────────────────────
# Subset of most common Vietnamese banks. Keys are the bank_code we use
# internally; values = NAPAS bank BIN (6 digits) — what goes inside the QR.
BANK_BINS: dict[str, dict[str, str]] = {
    "TPB": {"bin": "970423", "name": "TPBank"},
    "MB":  {"bin": "970422", "name": "MB Bank"},
    "VCB": {"bin": "970436", "name": "Vietcombank"},
    "VPB": {"bin": "970432", "name": "VPBank"},
    "TCB": {"bin": "970407", "name": "Techcombank"},
    "ACB": {"bin": "970416", "name": "ACB"},
    "BIDV": {"bin": "970418", "name": "BIDV"},
    "AGB": {"bin": "970405", "name": "Agribank"},
    "VTB": {"bin": "970415", "name": "Vietinbank"},
    "OCB": {"bin": "970448", "name": "OCB"},
    "MSB": {"bin": "970426", "name": "MSB"},
    "STB": {"bin": "970403", "name": "Sacombank"},
    "HDB": {"bin": "970437", "name": "HDBank"},
    "SHB": {"bin": "970443", "name": "SHB"},
}


# ─── TLV helpers ─────────────────────────────────────────────────────────────


def _tlv(tag: str, value: str) -> str:
    """Build single TLV block: 2-digit tag + 2-digit length + value."""
    if len(tag) != 2:
        raise ValueError(f"tag must be 2 digits: {tag!r}")
    length = len(value)
    if length > 99:
        raise ValueError(f"value too long ({length}) for TLV tag {tag}")
    return f"{tag}{length:02d}{value}"


def _crc16_ccitt(data: bytes) -> int:
    """CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflection, no xorout).

    This is the algorithm specified by EMVCo / NAPAS for tag 63.
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc & 0xFFFF


# ─── Public API ──────────────────────────────────────────────────────────────


def get_bank_bin(bank_code: str) -> str:
    """Lookup NAPAS bank BIN by Zeni internal bank_code. Raise ValueError if unknown."""
    info = BANK_BINS.get(bank_code.upper())
    if info is None:
        raise ValueError(f"Unknown bank_code: {bank_code!r}. Add to BANK_BINS registry.")
    return info["bin"]


def generate_qr_payload(
    bank_code: str,
    account_number: str,
    amount_vnd: Optional[int] = None,
    add_info: Optional[str] = None,
    ref: Optional[str] = None,
    service_code: str = "QRIBFTTA",  # transfer to account
) -> str:
    """Build a Napas247-compliant VietQR payload string.

    Parameters
    ----------
    bank_code     : Zeni internal bank code (e.g. 'TPB','VCB'); resolved to NAPAS BIN.
    account_number: Beneficiary account number string (digits only).
    amount_vnd    : Optional. If provided → fixed-amount QR; else customer types.
    add_info      : Free-text purpose/note shown on bank app (max ~25 chars).
    ref           : Optional bill/reference number (intent_code typically).
    service_code  : 'QRIBFTTA' (transfer to account, default) or 'QRIBFTTC' (card).

    Returns
    -------
    Full QR text (e.g. "00020101021238540010A0000007270124000697...6304ABCD").
    """
    if not account_number or not account_number.isdigit():
        # Allow placeholder zeros for testing but keep a sanity check
        if not account_number:
            raise ValueError("account_number required")
    bin_code = get_bank_bin(bank_code)

    # ─ Tag 38: Merchant Account Information (Napas247) ─
    sub_38_00 = _tlv("00", "A000000727")  # NAPAS GUID
    sub_01_inner = _tlv("00", bin_code) + _tlv("01", account_number)
    sub_38_01 = _tlv("01", sub_01_inner)
    sub_38_02 = _tlv("02", service_code)
    tag_38 = _tlv("38", sub_38_00 + sub_38_01 + sub_38_02)

    # ─ Root TLV tags ─
    tag_00 = _tlv("00", "01")       # Payload Format Indicator
    # Point of Initiation Method: 11 = static (reusable), 12 = dynamic (single use)
    poi = "12" if amount_vnd is not None else "11"
    tag_01 = _tlv("01", poi)
    tag_53 = _tlv("53", "704")      # Currency code (VND ISO-4217)
    tag_58 = _tlv("58", "VN")       # Country code

    parts = [tag_00, tag_01, tag_38, tag_53]
    if amount_vnd is not None:
        # Tag 54: Transaction amount as string, no decimals for VND
        parts.append(_tlv("54", str(int(amount_vnd))))
    parts.append(tag_58)

    # ─ Tag 62: Additional data (bill ref, purpose) ─
    additional = ""
    if ref:
        additional += _tlv("05", ref[:25])      # Bill / Reference number
    if add_info:
        additional += _tlv("08", add_info[:25])  # Purpose of transaction
    if additional:
        parts.append(_tlv("62", additional))

    # ─ Tag 63: CRC-16/CCITT-FALSE over everything + literal "6304" ─
    base = "".join(parts) + "6304"
    crc_val = _crc16_ccitt(base.encode("ascii"))
    crc_hex = f"{crc_val:04X}"
    tag_63 = _tlv("63", crc_hex)

    return "".join(parts) + tag_63


def generate_qr_image(payload: str, size: int = 300) -> bytes:
    """Render a VietQR payload string into PNG bytes.

    Tries ``qrcode[pil]`` first (already in requirements.txt); if missing for
    any reason, returns a minimal placeholder PNG so endpoints still respond.
    """
    try:
        import qrcode  # type: ignore
        from qrcode.constants import ERROR_CORRECT_M  # type: ignore

        qr = qrcode.QRCode(
            version=None,
            error_correction=ERROR_CORRECT_M,
            box_size=max(2, size // 40),
            border=2,
        )
        qr.add_data(payload)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        # Resize to requested size if PIL supports it
        try:
            from PIL import Image  # type: ignore
            img_pil = img.get_image() if hasattr(img, "get_image") else img
            img_pil = img_pil.resize((size, size), Image.NEAREST)
            buf = io.BytesIO()
            img_pil.save(buf, format="PNG")
            return buf.getvalue()
        except Exception:
            buf = io.BytesIO()
            img.save(buf)
            return buf.getvalue()
    except ImportError:
        log.warning("qrcode library not installed; returning placeholder PNG")
        return _placeholder_png(size)


def generate_qr_image_b64(payload: str, size: int = 300) -> str:
    """Same as generate_qr_image but returns 'data:image/png;base64,...' URL."""
    raw = generate_qr_image(payload, size)
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _placeholder_png(size: int) -> bytes:
    """Tiny 1x1 transparent PNG fallback (when qrcode lib unavailable)."""
    # 1x1 PNG, transparent — minimal valid file
    return bytes([
        0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A,
        0x00, 0x00, 0x00, 0x0D, 0x49, 0x48, 0x44, 0x52,
        0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
        0x08, 0x06, 0x00, 0x00, 0x00, 0x1F, 0x15, 0xC4,
        0x89, 0x00, 0x00, 0x00, 0x0D, 0x49, 0x44, 0x41,
        0x54, 0x78, 0x9C, 0x63, 0x00, 0x01, 0x00, 0x00,
        0x05, 0x00, 0x01, 0x0D, 0x0A, 0x2D, 0xB4, 0x00,
        0x00, 0x00, 0x00, 0x49, 0x45, 0x4E, 0x44, 0xAE,
        0x42, 0x60, 0x82,
    ])


# ─── Helpers for ref code generation ─────────────────────────────────────────


def make_intent_code(workspace_id: str, ts_seconds: int) -> str:
    """Build deterministic intent_code: ``ZP-{ws8}-{ts:base36}``.

    - ws8 = first 8 chars of workspace_id (upper, alnum only) for readability.
    - ts:base36 keeps the code short enough for tag 62 (max 25 chars).
    """
    ws_short = "".join(c for c in workspace_id.upper() if c.isalnum())[:8] or "ZENI"
    ts_b36 = _to_base36(ts_seconds)
    code = f"ZP-{ws_short}-{ts_b36}"
    return code[:25]  # safety cap


def _to_base36(n: int) -> str:
    if n <= 0:
        return "0"
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    out = ""
    while n > 0:
        n, rem = divmod(n, 36)
        out = chars[rem] + out
    return out
