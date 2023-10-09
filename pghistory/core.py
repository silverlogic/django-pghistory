"""Core functionality and interface of pghistory"""
import copy
import re
import sys
import warnings

import pgtrigger
from django.apps import apps
from django.db import connections, models
from django.db.models import sql
from django.db.models.fields.related import RelatedField
from django.db.models.sql import compiler
from django.utils.module_loading import import_string

from pghistory import config, constants, trigger, utils

if utils.psycopg_maj_version == 2:
    from psycopg2.extensions import AsIs as Literal
elif utils.psycopg_maj_version == 3:
    import psycopg.adapt

    class Literal:
        def __init__(self, val):
            self.val = val

    class LiteralDumper(psycopg.adapt.Dumper):
        def dump(self, obj):
            return obj.val.encode("utf-8")

        def quote(self, obj):
            return self.dump(obj)

else:
    raise AssertionError


_registered_trackers = {}


def _fmt_trigger_name(label):
    """Given a history event label, generate a trigger name"""
    if label:
        return re.sub("[^0-9a-zA-Z]+", "_", label)
    else:  # pragma: no cover
        return None


def _get_tracker_type_from_class(tracker_class):
    return re.sub(r"(?<!^)(?=[A-Z])", "_", tracker_class.__name__).lower()


class Tracker:
    """For tracking an event when a condition happens on a model."""

    label = None
    type = None

    def __init__(self, label=None):
        self.label = label or self.label or self.__class__.__name__.lower()
        self.type = _get_tracker_type_from_class(self.__class__)

    def setup(self, event_model):
        """Set up the tracker for the event model"""
        pass

    def register(self, event_model):
        """Registers the tracker for the event model and calls user-defined setup"""
        tracked_model = event_model.pgh_tracked_model

        if (tracked_model, self.label, self.type) in _registered_trackers:
            raise ValueError(
                f'Tracker with label "{self.label}" and type "{self.type}" already exists'
                f' for model "{tracked_model._meta.label}". Supply a'
                " different label as the first argument to the tracker."
            )

        _registered_trackers[(tracked_model, self.label, self.type)] = event_model

    def pghistory_setup(self, event_model):
        self.register(event_model)
        self.setup(event_model)


class ManualTracker(Tracker):
    """For manually tracking an event."""


class Event(Tracker):
    """The deprecated base class for event trackers. Use `Tracker` instead"""

    def __init__(self, label=None):
        warnings.warn(
            "The django-pghistory 'Event' class is deprecated and renamed to 'Tracker'.",
            DeprecationWarning,
        )
        super().__init__(label=label)


class DatabaseTracker(Tracker):
    """For tracking an event automatically based on database changes."""

    when = None
    condition = None
    operation = None
    snapshot = None

    def __init__(
        self,
        label=None,
        *,
        when=None,
        condition=None,
        operation=None,
        snapshot=None,
        extra_context=None,
    ):
        super().__init__(label=label)

        self.when = when or self.when
        self.condition = condition or self.condition
        self.operation = operation or self.operation
        self.snapshot = snapshot or self.snapshot
        self.extra_context = extra_context or {}

    def add_event_trigger(
        self,
        *,
        event_model,
        label,
        snapshot,
        when,
        operation,
        condition=None,
        name=None,
        extra_context={},
    ):
        pgtrigger.register(
            trigger.Event(
                event_model=event_model,
                label=label,
                name=_fmt_trigger_name(name or label),
                snapshot=snapshot,
                when=when,
                operation=operation,
                condition=condition,
                extra_context=extra_context,
            )
        )(event_model.pgh_tracked_model)

    def setup(self, event_model):
        self.add_event_trigger(
            event_model=event_model,
            label=self.label,
            snapshot=self.snapshot,
            when=self.when,
            operation=self.operation,
            condition=self.condition,
            extra_context=self.extra_context,
        )


class DatabaseEvent(DatabaseTracker):
    """
    The deprecated base class for all trigger-based trackers.
    Use `DatabaseTracker` instead.
    """

    def __init__(
        self,
        label=None,
        *,
        when=None,
        condition=None,
        operation=None,
        snapshot=None,
    ):  # pragma: no cover
        warnings.warn(
            "The django-pghistory 'DatabaseEvent' class is deprecated and renamed to"
            " 'DatabaseTracker'.",
            DeprecationWarning,
        )
        super().__init__(
            label=label,
            when=when,
            condition=condition,
            operation=operation,
            snapshot=snapshot,
        )


class Changed(pgtrigger.Condition):
    """A utilty to create conditions based on changes in the tracked model.

    Given the event model, we create a condition as follows:

    - If the event model trackes every field from the main model, we can
      use a standard ``OLD.* IS DISTINCT FROM NEW.*`` condition to snapshot
      every change on the main model.
    - If the event model tracks a subset of the fields of the main model,
      only changes to event fields will trigger a snapshot. In other words,
      if the main model has an int and char field, but the event model only
      tracks the char field, the condition will be
      ``OLD.char_field IS DISTINCT FROM NEW.char_field``.
    - If one has fields on the event model and wishes to ignore them from
      triggering snapshots, pass them to the ``exclude`` argument to this
      utility.
    """

    def __init__(self, event_model, exclude=None):
        self.event_model = event_model
        self.exclude = exclude or []

    def resolve(self, model):
        event_fields = [
            field.name
            for field in self.event_model._meta.fields
            if not field.name.startswith("pgh_")
        ]
        model_fields = [f.name for f in model._meta.fields]

        # By default, any field in both the main model and event model that
        # change will trigger the condition. You can exclude fields from
        # the event model that will trigger snapshots.
        conditional_fields = [f for f in event_fields if f not in self.exclude]

        if set(event_fields) == set(model_fields) == set(conditional_fields):
            # We're tracking every field on any change
            condition = pgtrigger.Condition("OLD.* IS DISTINCT FROM NEW.*")
        else:
            # We're either tracking a subset of fields or we have
            condition = pgtrigger.Q()

            for field in conditional_fields:
                if hasattr(model, field):
                    condition |= pgtrigger.Q(**{f"old__{field}__df": pgtrigger.F(f"new__{field}")})

        return condition.resolve(model)


class SnapshotInsert(DatabaseTracker):
    """
    Tracks changes to fields.
    A snapshot tracker tracks inserts. It ensures that no
    duplicate rows are created with a pre-configured condition.
    """

    label = "snapshot"

    def __init__(self, label=None, delayed=False):
        self.delayed = delayed
        return super().__init__(label=label)

    def setup(self, event_model):
        self.add_event_trigger(
            event_model=event_model,
            label=self.label,
            name=f"{self.label}_insert",
            snapshot="NEW",
            when=pgtrigger.After,
            operation=pgtrigger.Insert,
        )


class SnapshotUpdate(DatabaseTracker):
    """
    Tracks changes to fields.
    A snapshot tracker tracks updates. It ensures that no
    duplicate rows are created with a pre-configured condition.
    """

    label = "snapshot"

    def __init__(self, label=None, delayed=False):
        self.delayed = delayed
        return super().__init__(label=label)

    def setup(self, event_model):
        self.add_event_trigger(
            event_model=event_model,
            label=self.label,
            name=f"{self.label}_update",
            snapshot="NEW",
            when=pgtrigger.After,
            operation=pgtrigger.Update,
            condition=Changed(event_model),
        )


class SnapshotDelete(DatabaseTracker):
    """
    Tracks changes to fields.
    A snapshot tracker tracks deletes. It ensures that no
    duplicate rows are created with a pre-configured condition.
    """

    label = "snapshot"

    def __init__(self, label=None, delayed=False):
        self.delayed = delayed
        return super().__init__(label=label)

    def setup(self, event_model):
        self.add_event_trigger(
            event_model=event_model,
            label=self.label,
            name=f"{self.label}_delete",
            snapshot="OLD",
            when=pgtrigger.After,
            operation=pgtrigger.Delete,
        )


class Snapshot(DatabaseTracker):
    """
    NOTE: Two triggers are created since Insert triggers do
    not allow comparison against the OLD values. We could also
    place this in one trigger and do the condition in the plpgsql code.
    """

    def __init__(self, label=None, delayed=False):
        self.delayed = delayed
        return super().__init__(label=label)

    def pghistory_setup(self, event_model):
        self.register(event_model)

        SnapshotInsert(label=self.label).setup(event_model)
        SnapshotUpdate(label=self.label).setup(event_model)
        SnapshotDelete(label=self.label).setup(event_model)


class PreconfiguredDatabaseTracker(DatabaseTracker):
    """
    A base database tracker that only takes a condition. Subclasses
    preconfigure the other parameters
    """

    def __init__(self, label=None, *, condition=None, extra_context=None):
        return super().__init__(label=label, condition=condition, extra_context=extra_context)


class AfterInsertOrUpdate(PreconfiguredDatabaseTracker):
    """
    A database tracker that happens after insert/update
    """

    operation = pgtrigger.Insert | pgtrigger.Update
    snapshot = "NEW"


class AfterInsert(PreconfiguredDatabaseTracker):
    """For trackers that fire after a database insert"""

    operation = pgtrigger.Insert
    snapshot = "NEW"


class BeforeUpdate(PreconfiguredDatabaseTracker):
    """
    For trackers that fire before a database update. The OLD values of the row
    will be snapshot to the event model
    """

    operation = pgtrigger.Update
    snapshot = "OLD"


class AfterUpdate(PreconfiguredDatabaseTracker):
    """
    For trackers that fire after a database update. The NEW values of the row
    will be snapshot to the event model
    """

    operation = pgtrigger.Update
    snapshot = "NEW"


class BeforeDelete(PreconfiguredDatabaseTracker):
    """
    For trackers that fire before a database deletion.
    """

    operation = pgtrigger.Delete
    snapshot = "OLD"


class BeforeUpdateOrDelete(PreconfiguredDatabaseTracker):
    """
    A database tracker that snapshots the old row during an update or delete
    """

    operation = pgtrigger.Update | pgtrigger.Delete
    snapshot = "OLD"


def _pascalcase(string):
    """Convert string into pascal case."""

    string = re.sub(r"^[\-_\.]", "", str(string))
    if not string:  # pragma: no branch
        return string

    return string[0].upper() + re.sub(
        r"[\-_\.\s]([a-z])",
        lambda matched: matched.group(1).upper(),
        string[1:],
    )


def _generate_event_model_name(base_model, tracked_model, fields):
    """Generates a default history model name"""
    name = tracked_model._meta.object_name
    if fields:
        name += "_" + "_".join(fields)

    name += f"_{base_model._meta.object_name.lower()}"
    return _pascalcase(name)


def _get_field_construction(field):
    _, _, args, kwargs = field.deconstruct()

    if isinstance(field, models.ForeignKey):
        default = config.foreign_key_field()
    elif isinstance(field, RelatedField):  # pragma: no cover
        default = config.related_field()
    else:
        default = config.field()

    kwargs.update(default.kwargs)

    cls = field.__class__
    if isinstance(field, models.OneToOneField):
        cls = models.ForeignKey
    elif isinstance(field, models.FileField):
        kwargs.pop("primary_key", None)

    for field_class, exclude_kwargs in config.exclude_field_kwargs().items():
        if isinstance(field, field_class):
            for exclude_kwarg in exclude_kwargs:
                kwargs.pop(exclude_kwarg, None)

    return cls, args, kwargs


def _generate_history_field(tracked_model, field):
    """
    When generating a history model from a tracked model, ensure the fields
    are set up properly so that related names and other information
    from the tracked model do not cause errors.
    """
    field = tracked_model._meta.get_field(field)

    if isinstance(field, models.AutoField):
        return models.IntegerField()
    elif isinstance(field, models.BigAutoField):  # pragma: no cover
        return models.BigIntegerField()
    elif not field.concrete:  # pragma: no cover
        # Django doesn't have any non-concrete fields that appear
        # in ._meta.fields, but packages like django-prices have
        # non-concrete fields
        return field

    # The "swappable" field causes issues during deconstruct()
    # since it tries to load models. Patch it and set it back to the original
    # value later
    field = copy.deepcopy(field)
    swappable = getattr(field, "swappable", constants.UNSET)
    field.swappable = False
    cls, args, kwargs = _get_field_construction(field)
    field = cls(*args, **kwargs)

    if swappable is not constants.UNSET:
        field.swappable = swappable

    return field


def _generate_related_name(base_model, fields):
    """
    Generates a related name to the tracking model based on the base
    model and traked fields
    """
    related_name = base_model._meta.object_name.lower()
    return "_".join(fields) + f"_{related_name}" if fields else related_name


def _validate_event_model_path(*, app_label, model_name, abstract):
    if app_label not in apps.app_configs:
        raise ValueError(f'App label "{app_label}" is invalid')

    app = apps.app_configs[app_label]
    models_module = app.module.__name__ + ".models"
    if not abstract and hasattr(sys.modules[models_module], model_name):
        raise ValueError(
            f"App {app_label} already has {model_name} model. You must"
            " explicitly declare an unused model name for the pghistory model."
        )
    elif models_module.startswith("django."):
        raise ValueError(
            "A history model cannot be generated under third party app"
            f' "{app_label}". You must explicitly pass an app label'
            " when configuring tracking."
        )


def _get_obj_field(*, obj_field, tracked_model, obj_fk, related_name, base_model, fields):
    if obj_fk is not constants.UNSET:
        warnings.warn(
            "The django-pghistory 'obj_fk' argument is deprecated. Use 'obj_field' instead.",
            DeprecationWarning,
        )
        return obj_fk
    elif obj_field is None:  # pragma: no cover
        return None
    elif obj_field is constants.UNSET:
        obj_field = config.obj_field()

        if related_name is not None:
            warnings.warn(
                "The django-pghistory 'related_name' argument is deprecated. Use the"
                " 'related_name' option of 'obj_field' instead.",
                DeprecationWarning,
            )

        if obj_field._kwargs.get("related_name", constants.DEFAULT) == constants.DEFAULT:
            obj_field._kwargs["related_name"] = related_name or _generate_related_name(
                base_model, fields
            )

    if isinstance(obj_field, config.ObjForeignKey):
        return models.ForeignKey(tracked_model, **obj_field.kwargs)
    else:  # pragma: no cover
        raise TypeError("obj_field must be of type pghistory.ObjForeignKey.")


def _get_context_field(*, context_field, context_fk):
    if context_fk is not constants.UNSET:
        warnings.warn(
            "The django-pghistory 'context_fk' argument is deprecated. Use "
            "'context_field' instead.",
            DeprecationWarning,
        )
        return context_fk
    elif context_field is None:  # pragma: no cover
        return None
    elif context_field is constants.UNSET:
        context_field = config.context_field()

    if isinstance(context_field, config.ContextForeignKey):
        return models.ForeignKey("pghistory.Context", **context_field.kwargs)
    elif isinstance(context_field, config.ContextJSONField):
        return utils.JSONField(**context_field.kwargs)
    else:  # pragma: no cover
        raise TypeError(
            "context_field must be of type pghistory.ContextForeignKey"
            " or pghistory.ContextJSONField."
        )


def _get_context_id_field(*, context_id_field):
    if context_id_field is None:
        return None
    elif context_id_field is constants.UNSET:  # pragma: no branch
        context_id_field = config.context_id_field()

    if isinstance(context_id_field, config.ContextUUIDField):
        return models.UUIDField(**context_id_field.kwargs)
    else:  # pragma: no cover
        raise TypeError("context_id_field must be of type pghistory.ContextUUIDField.")


def create_event_model(
    tracked_model,
    *trackers,
    fields=None,
    exclude=None,
    obj_fk=constants.UNSET,
    context_fk=constants.UNSET,
    obj_field=constants.UNSET,
    context_field=constants.UNSET,
    context_id_field=constants.UNSET,
    related_name=None,
    name=None,
    model_name=None,
    app_label=None,
    base_model=None,
    attrs=None,
    meta=None,
    abstract=True,
):
    """
    Obtain a base event model.

    Instead of using `pghistory.track`, which dynamically generates an event
    model, one can instead construct a event model themselves, which
    will also set up event tracking for the tracked model.

    Args:
        tracked_model (models.Model): The model that is being tracked.
        *trackers (List[`Tracker`]): The event trackers. When using any tracker that
            inherits `pghistory.DatabaseTracker`, such as
            `pghistory.AfterInsert`, a Postgres trigger will be installed that
            automatically tracks the event with a generated event model. Trackers
            that do not inherit `pghistory.DatabaseTracker` are assumed to have
            manual events created by the user.
        fields (List[str], default=None): The list of fields to snapshot
            when the event takes place. When no fields are provided, the entire
            model is snapshot when the event happens. Note that snapshotting
            of the OLD or NEW row is configured by the ``snapshot``
            attribute of the `DatabaseTracker` object. Manual events must specify
            these fields during manual creation.
        exclude (List[str], default=None): Instead of providing a list
            of fields to snapshot, a user can instead provide a list of fields
            to not snapshot.
        obj_field (pghistory.ObjForeignKey, default=unset): The foreign key field
            configuration that references the tracked object. Defaults to an
            unconstrained non-nullable foreign key. Use ``None`` to create a event model
            with no reference to the tracked object.
        context_field (Union[pghistory.ContextForeignKey, pghistory.ContextJSONField], default=unset):
            The context field configuration. Defaults to a nullable unconstrained foreign key.
            Use ``None`` to avoid attaching historical context altogether.
        context_id_field (pghistory.ContextUUIDField, default=unset): The context ID field
            configuration when using a ContextJSONField for the context_field. When using
            a denormalized context field, the ID field is used to track the UUID of the
            context. Use ``None`` to avoid using this field for denormalized context.
        model_name (str, default=None): Use a custom model name
            when the event model is generated. Otherwise a default
            name based on the tracked model and fields will be created.
        app_label (str, default=None): The app_label for the generated
            event model. Defaults to the app_label of the tracked model. Note,
            when tracking a Django model (User) or a model of a third-party
            app, one must manually specify the app_label of an internal app to
            use so that migrations work properly.
        base_model (models.Model, default=pghistory.models.Event): The base model for the event
            model. Must inherit pghistory.models.Event.
        attrs (dict, default=None): Additional attributes to add to the event model
        meta (dict, default=None): Additional attributes to add to the Meta class of the
            event model.
        abstract (bool, default=True): ``True`` if the generated model should
            be an abstract model.


    Example:
        Create a manual event model::

            class MyEventModel(create_event_model(
                TrackedModel,
                pghistory.AfterInsert('model_create'),
            )):
                # Add custom indices or change default field declarations...
    """  # noqa
    event_model = import_string("pghistory.models.Event")
    base_model = base_model or config.base_model()
    assert issubclass(base_model, event_model)

    obj_field = _get_obj_field(
        obj_field=obj_field,
        tracked_model=tracked_model,
        obj_fk=obj_fk,
        related_name=related_name,
        base_model=base_model,
        fields=fields,
    )
    context_field = _get_context_field(context_field=context_field, context_fk=context_fk)
    context_id_field = _get_context_id_field(context_id_field=context_id_field)

    if name is not None:  # pragma: no cover
        warnings.warn(
            "The 'name' argument for pghistory.create_event_model is"
            " deprecated. Use the 'model_name' argument",
            DeprecationWarning,
        )
        model_name = name

    model_name = model_name or _generate_event_model_name(base_model, tracked_model, fields)
    app_label = app_label or tracked_model._meta.app_label
    _validate_event_model_path(app_label=app_label, model_name=model_name, abstract=abstract)
    app = apps.app_configs[app_label]
    models_module = app.module.__name__ + ".models"

    attrs = attrs or {}
    attrs.update({"pgh_trackers": trackers})
    meta = meta or {}
    exclude = exclude or []
    fields = fields or [f.name for f in tracked_model._meta.fields if f.name not in exclude]

    class_attrs = {
        "__module__": models_module,
        "Meta": type("Meta", (), {"abstract": abstract, "app_label": app_label, **meta}),
        "pgh_tracked_model": tracked_model,
        **{field: _generate_history_field(tracked_model, field) for field in fields},
        **attrs,
    }

    if isinstance(context_field, utils.JSONField) and context_id_field:
        class_attrs["pgh_context_id"] = context_id_field

    if context_field:
        class_attrs["pgh_context"] = context_field

    if obj_field:
        class_attrs["pgh_obj"] = obj_field

    event_model = type(model_name, (base_model,), class_attrs)
    if not abstract:
        setattr(sys.modules[models_module], model_name, event_model)

    return event_model


def get_event_model(*args, **kwargs):
    warnings.warn(
        "The django-pghistory 'get_event_model' function is deprecated. Use"
        " 'create_event_model' instead.",
        DeprecationWarning,
    )
    return create_event_model(*args, **kwargs)


def ProxyField(proxy, field):
    """
    Proxies a JSON field from a model and adds it as a field in the queryset.

    Args:
        proxy (str): The value to proxy, e.g. "user__email"
        field (Type[django.models.Field]): The field that will be used to cast
            the resulting value

    """
    if not isinstance(field, models.Field):  # pragma: no cover
        raise TypeError(f'"{field}" is not a Django model Field instace')

    field.pgh_proxy = proxy
    return field


def track(
    *trackers,
    fields=None,
    exclude=None,
    obj_fk=constants.UNSET,
    context_fk=constants.UNSET,
    obj_field=constants.UNSET,
    context_field=constants.UNSET,
    context_id_field=constants.UNSET,
    related_name=None,
    model_name=None,
    app_label=None,
    base_model=None,
    attrs=None,
    meta=None,
):
    """
    A decorator for tracking events for a model.

    When using this decorator, an event model is dynamically generated
    that snapshots the entire model or supplied fields of the model
    based on the ``events`` supplied. The snapshot is accompanied with
    the label that identifies the event.

    Args:
        *trackers (List[`Tracker`]): The event trackers. When using any tracker that
            inherits `pghistory.DatabaseTracker`, such as
            `pghistory.AfterInsert`, a Postgres trigger will be installed that
            automatically tracks the event with a generated event model. Trackers
            that do not inherit `pghistory.DatabaseTracker` are assumed to have
            manual events created by the user.
        fields (List[str], default=None): The list of fields to snapshot
            when the event takes place. When no fields are provided, the entire
            model is snapshot when the event happens. Note that snapshotting
            of the OLD or NEW row is configured by the ``snapshot``
            attribute of the `DatabaseTracker` object. Manual events must specify
            these fields during manual creation.
        exclude (List[str], default=None): Instead of providing a list
            of fields to snapshot, a user can instead provide a list of fields
            to not snapshot.
        obj_field (pghistory.ObjForeignKey, default=unset): The foreign key field
            configuration that references the tracked object. Defaults to an
            unconstrained non-nullable foreign key. Use ``None`` to create a event model
            with no reference to the tracked object.
        context_field (Union[pghistory.ContextForeignKey, pghistory.ContextJSONField], default=unset):
            The context field configuration. Defaults to a nullable unconstrained foreign key.
            Use ``None`` to avoid attaching historical context altogether.
        context_id_field (pghistory.ContextUUIDField, default=unset): The context ID field
            configuration when using a ContextJSONField for the context_field. When using
            a denormalized context field, the ID field is used to track the UUID of the
            context. Use ``None`` to avoid using this field for denormalized context.
        model_name (str, default=None): Use a custom model name
            when the event model is generated. Otherwise a default
            name based on the tracked model and fields will be created.
        app_label (str, default=None): The app_label for the generated
            event model. Defaults to the app_label of the tracked model. Note,
            when tracking a Django model (User) or a model of a third-party
            app, one must manually specify the app_label of an internal app to
            use so that migrations work properly.
        base_model (models.Model, default=`pghistory.models.Event`): The base model for the event
            model. Must inherit `pghistory.models.Event`.
        attrs (dict, default=None): Additional attributes to add to the event model
        meta (dict, default=None): Additional attributes to add to the Meta class of the
            event model.
    """  # noqa

    def _model_wrapper(model_class):
        create_event_model(
            model_class,
            *trackers,
            fields=fields,
            exclude=exclude,
            obj_fk=obj_fk,
            context_fk=context_fk,
            obj_field=obj_field,
            context_field=context_field,
            context_id_field=context_id_field,
            model_name=model_name,
            related_name=related_name,
            app_label=app_label,
            abstract=False,
            base_model=base_model,
            attrs=attrs,
            meta=meta,
        )

        return model_class

    return _model_wrapper


class _InsertEventCompiler(compiler.SQLInsertCompiler):
    def __init__(self, query, connection, using, event_model):
        self.event_model = event_model
        super().__init__(query, connection, using)

    def get_sql_param(self, field, param):
        if field.name != "pgh_context":
            return param
        if isinstance(self.event_model._meta.get_field("pgh_context"), utils.JSONField):
            return Literal(
                "COALESCE(NULLIF(CURRENT_SETTING('pghistory.context_metadata', TRUE), ''), NULL)::JSONB"
            )
        return Literal("_pgh_attach_context()")

    def as_sql(self, *args, **kwargs):
        ret = super().as_sql(*args, **kwargs)
        assert len(ret) == 1
        params = [
            self.get_sql_param(field, param) for field, param in zip(self.query.fields, ret[0][1])
        ]

        return [(ret[0][0], params)]


def create_event(obj, *, label, tracker_class, using="default"):
    """Manually create a event for an object.

    Events are automatically linked with any context being tracked
    via `pghistory.context`.

    Args:
        obj (models.Model): An instance of a model.
        label (str): The event label.
        tracker_class (Type[Tracker]): The tracker class that is tracking the event.
        using (str): The database

    Raises:
        ValueError: If the event label has not been registered for the model.

    Returns:
        models.Model: The created event model.
    """
    tracker_type = _get_tracker_type_from_class(tracker_class)

    # Verify that the provided label is tracked
    if (obj.__class__, label, tracker_type) not in _registered_trackers:
        raise ValueError(
            f'"{label}" of type "{tracker_type}" is not a registered tracker for model {obj._meta.object_name}.'
        )

    event_model = _registered_trackers[(obj.__class__, label, tracker_type)]
    event_model_kwargs = {
        "pgh_label": label,
        **{
            field.attname: getattr(obj, field.attname)
            for field in event_model._meta.fields
            if not field.name.startswith("pgh_")
        },
    }
    if hasattr(event_model, "pgh_obj"):
        event_model_kwargs["pgh_obj"] = obj

    event_obj = event_model(**event_model_kwargs)

    # The event model is inserted manually with a custom SQL compiler
    # that attaches the context using the _pgh_attach_context
    # stored procedure. Django does not allow one to use F()
    # objects to reference stored procedures, so we have to
    # inject it with a custom SQL compiler here.
    query = sql.InsertQuery(event_model)
    query.insert_values(
        [field for field in event_model._meta.fields if not isinstance(field, models.AutoField)],
        [event_obj],
    )

    if utils.psycopg_maj_version == 3:
        connections[using].connection.adapters.register_dumper(Literal, LiteralDumper)

    vals = _InsertEventCompiler(
        query, connections[using], using=using, event_model=event_model
    ).execute_sql(event_model._meta.fields)

    # Django <= 2.2 does not support returning fields from a bulk create,
    # which requires us to fetch fields again to populate the context
    if isinstance(vals, int):  # pragma: no cover
        return event_model.objects.get(pgh_id=vals)
    else:
        # Django >= 3.1 returns the values as a list of one element
        if isinstance(vals, list) and len(vals) == 1:  # pragma: no branch
            vals = vals[0]

        for field, val in zip(event_model._meta.fields, vals):
            setattr(event_obj, field.attname, val)

        return event_obj


def event_models(
    models=None, references_model=None, tracks_model=None, include_missing_pgh_obj=False
):
    """
    Retrieve and filter all events models.

    Args:
        models (List[Model], default=None): The starting list of event models.
        references_model (Model, default=None): Filter by event models that reference this model.
        tracks_model (Model, default=None): Filter by models that directly track this model
            and have pgh_obj fields
        including_missing_pgh_obj (bool, default=False): Return tracked models even if the pgh_obj
            field is not available.
    """
    from pghistory.models import BaseAggregateEvent, Event  # noqa

    models = models or [
        model
        for model in apps.get_models()
        if issubclass(model, Event)
        and not issubclass(model, BaseAggregateEvent)
        and not model._meta.abstract
        and not model._meta.proxy
        and model._meta.managed
    ]

    if references_model:
        models = [
            model
            for model in models
            if any(utils.related_model(field) == references_model for field in model._meta.fields)
        ]

    if tracks_model and not include_missing_pgh_obj:
        models = [
            model
            for model in models
            if "pgh_obj" in (f.name for f in model._meta.fields)
            and utils.related_model(model._meta.get_field("pgh_obj")) == tracks_model
        ]
    elif tracks_model and include_missing_pgh_obj:
        models = [
            model
            for model in models
            if model.pgh_tracked_model._meta.concrete_model == tracks_model
        ]

    return models
