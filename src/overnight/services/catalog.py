from collections.abc import Mapping
from types import MappingProxyType

from overnight.services.calendar import Calendar
from overnight.services.chat import Chat
from overnight.services.mail import Mail
from overnight.services.operations import ServiceBoundary
from overnight.services.records import Records
from overnight.services.shop import Shop

BUILTIN_SERVICES: Mapping[str, ServiceBoundary] = MappingProxyType(
    {
        "mail": Mail(),
        "chat": Chat(),
        "calendar": Calendar(),
        "shop": Shop(),
        "records": Records(),
    }
)
