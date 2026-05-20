from __future__ import annotations


class BusinessProfileIncomplete(Exception):
    """Raised when a required tenant business-profile key is missing.

    Imported by ``apps.filing_automation.services.adapter`` and re-exported
    from there so both module paths refer to the same class identity.
    """

    def __init__(self, field: str):
        super().__init__(f"Missing required profile field: {field}")
        self.field = field


RRC_W3A_REQUIRED = [
    "rrc.w3a.cementing_company_name",
    "rrc.w3a.contact_phone",
    "rrc.w3a.contact_email",
    "rrc.w3a.submitter_default_name",
    "rrc.w3a.submitter_default_title",
]

RRC_W3A_OPTIONAL = [
    "rrc.w3a.default_plugging_date_offset_days",
    "rrc.w3a.cementing_company_address",
    "rrc.w3a.cementing_company_p5",
    "rrc.w3a.contact_ext",
]

SCHEMAS = {
    ("rrc", "w3a"): {"required": RRC_W3A_REQUIRED, "optional": RRC_W3A_OPTIONAL},
}

# Keys whose values must be strings when present (dotted suffix → short key name).
_RRC_W3A_STRING_FIELDS: dict[str, str] = {
    "cementing_company_name": "cementing_company_name",
    "contact_phone": "contact_phone",
    "contact_email": "contact_email",
    "submitter_default_name": "submitter_default_name",
    "submitter_default_title": "submitter_default_title",
    "cementing_company_address": "cementing_company_address",
    "cementing_company_p5": "cementing_company_p5",
    "contact_ext": "contact_ext",
}


def get_schema(agency: str, form: str) -> dict | None:
    return SCHEMAS.get((agency.lower(), form.lower()))


def validate_profile_types(agency: str, form: str, data_dict: dict) -> None:
    """Validate that string fields in *data_dict* are actually strings.

    *data_dict* must use the short key names (e.g. ``"cementing_company_p5"``,
    not the dotted path ``"rrc.w3a.cementing_company_p5"``).

    Raises:
        TypeError: when a field expected to be a string receives a non-string
            value (e.g. an int).
        ValueError: when ``(agency, form)`` is not a recognised schema.
    """
    schema = get_schema(agency, form)
    if schema is None:
        raise ValueError(f"Unknown schema for agency={agency!r} form={form!r}")

    for key, label in _RRC_W3A_STRING_FIELDS.items():
        if key in data_dict and data_dict[key] is not None:
            if not isinstance(data_dict[key], str):
                raise TypeError(
                    f"Profile field '{label}' must be a str, "
                    f"got {type(data_dict[key]).__name__!r}: {data_dict[key]!r}"
                )
