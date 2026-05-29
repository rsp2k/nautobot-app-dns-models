"""Plugin declaration for nautobot_dns_models."""

from importlib import metadata

from nautobot.apps import ConstanceConfigItem, NautobotAppConfig

# NOTE: The distribution name on PyPI differs from the import path -- this is
# a fork of upstream `nautobot-dns-models` distributed as
# `nautobot-dns-models-bitemporal` to avoid namespace collision. Don't use
# `metadata.version(__name__)` here; __name__ is the import path
# ("nautobot_dns_models") which doesn't match the distribution name.
__version__ = metadata.version("nautobot-dns-models-bitemporal")

# nautobot.core.cli places preprocessed settings in a nautobot_config module before loading django
# doing this is a hack but it's the only way we can modify the settings before NautobotAppConfig.constance_config is processed
try:
    import nautobot_config as settings

    NAUTOBOT_CONFIG_LOADED = True
except ImportError:
    NAUTOBOT_CONFIG_LOADED = False

constance_additional_fields = {
    "show_dns_panel": [
        "django.forms.fields.ChoiceField",
        {
            "widget": "django.forms.Select",
            "choices": [
                ("always", "Always"),
                ("if_present", "If records are present"),
                ("never", "Never"),
            ],
        },
    ],
    "dns_validation_level": [
        "django.forms.fields.ChoiceField",
        {
            "widget": "django.forms.Select",
            "choices": [
                ("none", "Disabled"),
                ("wire-format", "Wire format"),
            ],
        },
    ],
}

if NAUTOBOT_CONFIG_LOADED:
    # pylint:disable=no-member
    settings.CONSTANCE_ADDITIONAL_FIELDS |= constance_additional_fields


class NautobotDnsModelsConfig(NautobotAppConfig):
    """Plugin configuration for the nautobot_dns_models plugin."""

    name = "nautobot_dns_models"
    verbose_name = "Nautobot DNS Models"
    version = __version__
    author = "Network to Code, LLC"
    description = "Nautobot DNS Models."
    base_url = "dns"
    required_settings = []
    default_settings = {}
    docs_view_name = "plugins:nautobot_dns_models:docs"
    searchable_models = [
        "DNSView",
        "DNSRegistrar",
        "DNSRegistration",
        "DNSZone",
        "ARecord",
        "AAAARecord",
        "PTRRecord",
        "CNAMERecord",
        "NSRecord",
        "MXRecord",
        "SRVRecord",
        "TXTRecord",
    ]
    constance_config = {
        "SHOW_FORWARD_PANEL": ConstanceConfigItem(
            default="Always",
            help_text="Show A/AAAA Records panel in IP Address detailed view.",
            field_type="show_dns_panel",
        ),
        "SHOW_REVERSE_PANEL": ConstanceConfigItem(
            default="Always",
            help_text="Show PTR Records panel in IP Address detailed view.",
            field_type="show_dns_panel",
        ),
        "DNS_VALIDATION_LEVEL": ConstanceConfigItem(
            default="wire-format",
            help_text="DNS validation level for zones and records.",
            field_type="dns_validation_level",
        ),
        "CNAME_RESTRICTION_ENABLED": ConstanceConfigItem(
            default=False,
            help_text="Enforce CNAME exclusivity",
            field_type=bool,
        ),
    }


config = NautobotDnsModelsConfig  # pylint:disable=invalid-name
