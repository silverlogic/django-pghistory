import django

from pghistory.config import (
    ContextForeignKey,
    ContextJSONField,
    ContextUUIDField,
    Field,
    ForeignKey,
    ObjForeignKey,
    RelatedField,
)
from pghistory.constants import DEFAULT
from pghistory.core import (
    AfterInsert,
    AfterInsertOrUpdate,
    AfterUpdate,
    BeforeDelete,
    BeforeUpdate,
    BeforeUpdateOrDelete,
    Changed,
    create_event,
    create_event_model,
    DatabaseEvent,
    DatabaseTracker,
    Event,
    ManualTracker,
    ProxyField,
    Snapshot,
    track,
    Tracker,
)
from pghistory.runtime import context
from pghistory.version import __version__


__all__ = [
    "AfterInsert",
    "AfterInsertOrUpdate",
    "AfterUpdate",
    "BeforeDelete",
    "BeforeUpdate",
    "BeforeUpdateOrDelete",
    "Changed",
    "context",
    "ContextForeignKey",
    "ContextJSONField",
    "ContextUUIDField",
    "create_event",
    "create_event_model",
    "DatabaseEvent",
    "DatabaseTracker",
    "DEFAULT",
    "Event",
    "Field",
    "ForeignKey",
    "ManualTracker",
    "ObjForeignKey",
    "ProxyField",
    "RelatedField",
    "Snapshot",
    "track",
    "Tracker",
    "__version__",
]

if django.VERSION < (3, 2):  # pragma: no cover
    default_app_config = "pghistory.apps.PGHistoryConfig"

del django
