import pgtrigger
from django.contrib.auth.models import User
from django.db import models

import pghistory


@pghistory.track(pghistory.Snapshot())
class SnapshotImageField(models.Model):
    img_field = models.ImageField()


class UntrackedModel(models.Model):
    untracked = models.CharField(max_length=64)


@pghistory.track(
    pghistory.Snapshot(),
    context_field=pghistory.ContextJSONField(),
)
@pghistory.track(
    pghistory.Snapshot("snapshot_no_id"),
    obj_field=pghistory.ObjForeignKey(related_name="event_no_id"),
    context_field=pghistory.ContextJSONField(),
    context_id_field=None,
    model_name="DenormContextEventNoId",
)
class DenormContext(models.Model):
    """
    For testing denormalized context
    """

    int_field = models.IntegerField()
    fk_field = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True)


@pghistory.track(
    pghistory.Snapshot(),
    model_name="CustomModelSnapshot",
    related_name="snapshot",
)
@pghistory.track(
    pghistory.AfterUpdate(
        "int_field_updated",
        condition=pgtrigger.Q(old__int_field__df=pgtrigger.F("new__int_field")),
    )
)
class CustomModel(models.Model):
    """
    For testing history tracking with a custom primary key
    and custom column name
    """

    my_pk = models.UUIDField(primary_key=True)
    int_field = models.IntegerField(db_column="integer_field")


@pghistory.track(pghistory.Snapshot("snapshot"), related_name="snapshot")
class UniqueConstraintModel(models.Model):
    """For testing tracking models with unique constraints"""

    my_one_to_one = models.OneToOneField(CustomModel, on_delete=models.PROTECT)
    my_char_field = models.CharField(unique=True, max_length=32)
    my_int_field1 = models.IntegerField(db_index=True)
    my_int_field2 = models.IntegerField()

    class Meta:
        unique_together = [("my_int_field1", "my_int_field2")]


@pghistory.track(
    pghistory.Snapshot("dt_field_snapshot"),
    fields=["dt_field"],
    related_name="dt_field_snapshot",
)
@pghistory.track(
    pghistory.Snapshot("dt_field_int_field_snapshot"),
    fields=["dt_field", "int_field"],
    related_name="dt_field_int_field_snapshot",
)
@pghistory.track(
    pghistory.Snapshot("snapshot"),
    related_name="snapshot",
    model_name="SnapshotModelSnapshot",
)
@pghistory.track(
    pghistory.Snapshot("no_pgh_obj_snapshot"),
    obj_fk=None,
    related_name="no_pgh_obj_snapshot",
    model_name="NoPghObjSnapshot",
)
class SnapshotModel(models.Model):
    """
    For testing snapshots of a model or fields
    """

    dt_field = models.DateTimeField()
    int_field = models.IntegerField()
    fk_field = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True)


class CustomSnapshotModel(
    pghistory.create_event_model(
        SnapshotModel,
        pghistory.Snapshot("custom_snapshot"),
        exclude=["dt_field"],
        obj_fk=models.ForeignKey(
            SnapshotModel,
            related_name="custom_related_name",
            null=True,
            on_delete=models.DO_NOTHING,
            db_constraint=False,
        ),
        context_fk=None,
    )
):
    fk_field = models.ForeignKey("auth.User", on_delete=models.CASCADE, null=True)
    # Add an extra field that's not on the original model to try to throw
    # tests off
    fk_field2 = models.ForeignKey(
        "auth.User",
        db_constraint=False,
        null=True,
        on_delete=models.DO_NOTHING,
        related_name="+",
        related_query_name="+",
    )


@pghistory.track(
    pghistory.ManualTracker("manual_event"),
    pghistory.AfterInsert("model.create"),
    pghistory.BeforeUpdate("before_update"),
    pghistory.BeforeDelete("before_delete"),
    pghistory.AfterUpdate(
        "after_update",
        condition=pgtrigger.Q(old__dt_field__df=pgtrigger.F("new__dt_field")),
    ),
)
@pghistory.track(
    pghistory.Event("no_pgh_obj_manual_event"),
    obj_fk=None,
    model_name="NoPghObjEvent",
    related_name="no_pgh_obj_event",
)
class EventModel(models.Model):
    """
    For testing model events
    """

    dt_field = models.DateTimeField()
    int_field = models.IntegerField()


class CustomEventModel(
    pghistory.create_event_model(
        EventModel,
        pghistory.AfterInsert("model.custom_create"),
        fields=["dt_field"],
        context_fk=None,
        obj_fk=models.ForeignKey(
            EventModel,
            related_name="custom_related_name",
            null=True,
            on_delete=models.SET_NULL,
        ),
    )
):
    pass


CustomEventWithContext = pghistory.create_event_model(
    EventModel,
    pghistory.AfterInsert("model.custom_create_with_context"),
    abstract=False,
    name="CustomEventWithContext",
    obj_field=pghistory.ObjForeignKey(related_name="+"),
)


class CustomEventProxy(EventModel.pgh_event_models["model.create"]):
    url = pghistory.ProxyField("pgh_context__metadata__url", models.TextField(null=True))
    auth_user = pghistory.ProxyField(
        "pgh_context__metadata__user",
        models.ForeignKey("auth.User", on_delete=models.DO_NOTHING, null=True),
    )

    class Meta:
        proxy = True


class CustomAggregateEvent(pghistory.models.BaseAggregateEvent):
    user = models.ForeignKey("auth.User", on_delete=models.DO_NOTHING, null=True)
    url = models.TextField(null=True)

    class Meta:
        managed = False


class CustomEvents(pghistory.models.Events):
    user = models.ForeignKey("auth.User", on_delete=models.DO_NOTHING, null=True)
    url = pghistory.ProxyField("pgh_context__url", models.TextField(null=True))

    class Meta:
        proxy = True


@pghistory.track(
    pghistory.AfterInsert("group.add"),
    pghistory.BeforeDelete("group.remove"),
    obj_fk=None,
)
class UserGroups(User.groups.through):
    class Meta:
        proxy = True


# Test a custom tracker that snapshots before/after and ignores auto-fields in the condition
class IgnoreAutoFieldsSnapshot(pghistory.DatabaseTracker):
    """
    A custom tracker that snapshots OLD rows on update/delete. Snapshots are only created
    when manual fields are changed (i.e. auto_now fields are ignored in the condition)
    """

    def setup(self, event_model):
        exclude = [
            f.name
            for f in event_model.pgh_tracked_model._meta.fields
            if getattr(f, "auto_now", False) or getattr(f, "auto_now_add", False)
        ]

        self.add_event_trigger(
            event_model=event_model,
            label=self.label,
            name=f"{self.label}_update",
            snapshot="OLD",
            when=pgtrigger.After,
            operation=pgtrigger.Update,
            condition=pghistory.Changed(event_model, exclude=exclude),
        )

        self.add_event_trigger(
            event_model=event_model,
            label=self.label,
            name=f"{self.label}_delete",
            snapshot="OLD",
            when=pgtrigger.After,
            operation=pgtrigger.Delete,
        )


@pghistory.track(IgnoreAutoFieldsSnapshot(), related_name="no_auto_fields_event")
class IgnoreAutoFieldsSnapshotModel(models.Model):
    """For testing the IgnoreAutoFieldsSnapshot tracker"""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    my_char_field = models.CharField(max_length=32)
    my_int_field = models.IntegerField()
