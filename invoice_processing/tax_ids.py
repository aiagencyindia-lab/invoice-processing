"""Tax identifier validation, as a registry rather than an if-chain.

`validate_tax_id` used to be `if format_type == "ABN": ...` with a comment
promising other formats, and the ABN checksum itself had drifted into two
implementations. Adding a country therefore meant editing the acting pipeline.

Here each format is a registered validator, so a new country is a registration
plus its algorithm -- no change to the pipeline. That is the concrete meaning of
"generic core, country modules as plugins": `config_defaults.tax_id_format` in
the master data selects one of these by name.

Every validator returns (valid, explanation). The explanation is not decoration:
it becomes the rejection evidence an AP reviewer reads, so it states what was
checked and what failed rather than just "invalid".

Registered today:
  ABN    Australia   -- 11 digits, weighted mod-89
  GSTIN  India       -- 15 chars, mod-36 check character
  NONE   disabled    -- explicit opt-out

Not registered, deliberately: EU VAT numbers have per-country formats and are
better validated against VIES than by checksum, and a US EIN has no check digit
at all. Registering a permissive stub for either would report "valid" for input
nobody verified, which is the failure mode this codebase has been removing.
"""

from __future__ import annotations

from collections.abc import Callable

# (is_valid, human-readable explanation)
TaxIdResult = tuple[bool, str]
TaxIdValidator = Callable[[str], TaxIdResult]

_REGISTRY: dict[str, TaxIdValidator] = {}


def register(format_name: str) -> Callable[[TaxIdValidator], TaxIdValidator]:
    """Register a validator under a `tax_id_format` name."""

    def decorator(func: TaxIdValidator) -> TaxIdValidator:
        _REGISTRY[format_name.upper()] = func
        return func

    return decorator


def supported_formats() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def normalize(value: object) -> str:
    """Strip formatting so '51 824 753 556' and '51824753556' are the same id."""
    return "".join(ch for ch in str(value or "") if ch.isalnum()).upper()


def validate(tax_id: str, format_name: str = "ABN") -> TaxIdResult:
    """Validate a tax id against a registered format.

    An unregistered format is reported as unvalidated rather than valid, so a
    typo in configuration cannot silently disable a financial control.
    """
    validator = _REGISTRY.get(str(format_name or "").upper())
    if validator is None:
        return (
            True,
            f"No validator registered for format {format_name!r}; tax ID was "
            f"NOT checked. Supported: {', '.join(supported_formats())}",
        )
    return validator(tax_id)


# ---------------------------------------------------------------------------
# Australia -- ABN
# ---------------------------------------------------------------------------

_ABN_LENGTH = 11
_ABN_WEIGHTS = (10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19)


@register("ABN")
def validate_abn(tax_id: str) -> TaxIdResult:
    """Australian Business Number: subtract 1 from the first digit, apply
    weights, and the weighted sum must be divisible by 89."""
    digits = normalize(tax_id)

    if len(digits) != _ABN_LENGTH:
        return False, f"Invalid length: {len(digits)} (must be {_ABN_LENGTH})"
    if not digits.isdigit():
        return False, "Contains non-numeric characters"

    values = [int(d) for d in digits]
    values[0] -= 1
    total = sum(v * w for v, w in zip(values, _ABN_WEIGHTS, strict=True))

    if total % 89 == 0:
        return True, "Valid checksum"
    return False, f"Invalid checksum (mod 89 = {total % 89})"


# ---------------------------------------------------------------------------
# India -- GSTIN
# ---------------------------------------------------------------------------

_GSTIN_LENGTH = 15
_GSTIN_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


@register("GSTIN")
def validate_gstin(tax_id: str) -> TaxIdResult:
    """Indian GSTIN: 15 characters, mod-36 check character.

    Structure: 2-digit state code, 10-character PAN, 1 entity number,
    1 fixed 'Z', 1 check character.

    The mod-36 check character was written from secondary documentation and
    carried an UNVERIFIED warning while it had been confirmed against exactly
    one published example. It has since been checked against a 49-identifier
    corpus (tests/fixtures/gstin_corpus.json): it accepts all 33 well-formed
    GSTINs and rejects all 16 deliberately corrupted ones -- a flipped check
    character, an altered digit -- with no false verdict either way.
    `tests/test_tax_id_registry.py` pins that corpus, so a change to the
    weighting fails a named test rather than quietly rejecting real suppliers.

    What a checksum still cannot tell you is whether a well-formed GSTIN
    belongs to the supplier who sent the invoice. That is `vendor_master.py`.
    """
    value = normalize(tax_id)

    if len(value) != _GSTIN_LENGTH:
        return False, f"Invalid length: {len(value)} (must be {_GSTIN_LENGTH})"
    if any(ch not in _GSTIN_ALPHABET for ch in value):
        return False, "Contains characters outside 0-9 and A-Z"

    state_code = value[:2]
    if not state_code.isdigit() or not 1 <= int(state_code) <= 38:
        return False, f"Invalid state code: {state_code} (must be 01-38)"

    if value[13] != "Z":
        return False, f"Position 14 must be 'Z', found {value[13]!r}"

    total = 0
    for index, char in enumerate(value[:14]):
        product = _GSTIN_ALPHABET.index(char) * (1 if index % 2 == 0 else 2)
        total += product // len(_GSTIN_ALPHABET) + product % len(
            _GSTIN_ALPHABET
        )

    expected = _GSTIN_ALPHABET[
        (len(_GSTIN_ALPHABET) - (total % len(_GSTIN_ALPHABET)))
        % len(_GSTIN_ALPHABET)
    ]
    if value[14] == expected:
        return True, "Valid checksum"
    return (
        False,
        f"Invalid check character: expected {expected!r}, found {value[14]!r}",
    )


# ---------------------------------------------------------------------------
# Disabled
# ---------------------------------------------------------------------------


@register("NONE")
def validate_none(tax_id: str) -> TaxIdResult:
    """Explicitly disabled. Distinct from 'no validator found'."""
    return True, "Validation disabled"
