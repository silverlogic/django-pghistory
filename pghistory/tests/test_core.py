from contextlib import ExitStack as no_exception
import datetime as dt
import uuid

import ddf
from django.apps import apps
from django.db import models
from django.utils import timezone
import pytest

import pghistory
from pghistory import config
from pghistory import constants
import pghistory.core
import pghistory.tests.models as test_models


def test_generate_history_field(settings):
    """Test special cases of core._generate_history_field"""
    settings.PGHISTORY_EXCLUDE_FIELD_KWARGS = {models.ForeignKey: ["db_index", "db_constraint"]}
    field = pghistory.core._generate_history_field(test_models.SnapshotModel, "fk_field")
    assert field.db_constraint

    pghistory.core._generate_history_field(test_models.SnapshotModel, "int_field")


@pytest.mark.django_db
def test_image_field_snapshot():
    t = ddf.G(test_models.SnapshotImageField)
    assert t.event.count() == 1


def test_duplicate_registration():
    with pytest.raises(ValueError, match="already exists"):
        pghistory.Snapshot().pghistory_setup(test_models.SnapshotModelSnapshot)


def test_pgh_event_model():
    assert (
        test_models.UniqueConstraintModel.pgh_event_model.__name__ == "UniqueConstraintModelEvent"
    )

    with pytest.raises(ValueError, match="more than one"):
        test_models.SnapshotModel.pgh_event_model


def test_get_obj_field(settings):
    obj_field = pghistory.core._get_obj_field(
        tracked_model=test_models.SnapshotModel,
        base_model=config.base_model(),
        obj_field=constants.UNSET,
        obj_fk=constants.UNSET,
        fields=None,
        related_name=None,
    )
    assert obj_field.remote_field.related_name == "event"

    settings.PGHISTORY_OBJ_FIELD = pghistory.ObjForeignKey(related_name="hello")
    obj_field = pghistory.core._get_obj_field(
        tracked_model=test_models.SnapshotModel,
        base_model=config.base_model(),
        obj_field=constants.UNSET,
        obj_fk=constants.UNSET,
        fields=None,
        related_name=None,
    )
    assert obj_field.remote_field.related_name == "hello"


def test_get_event_model(mocker):
    patched_create_event_model = mocker.patch("pghistory.core.create_event_model", autospec=True)

    pghistory.core.get_event_model(test_models.SnapshotModel)
    patched_create_event_model.assert_called_once_with(test_models.SnapshotModel)


@pytest.mark.django_db
def test_denorm_context_tracking():
    """Test denormalized context tracking"""
    denorm_model = ddf.G(test_models.DenormContext)
    assert denorm_model.event.count() == 1

    event = denorm_model.event.first()
    assert event.pgh_context is None
    assert event.pgh_context_id is None

    event_no_id = denorm_model.event_no_id.first()
    assert event_no_id.pgh_context is None
    assert not hasattr(event_no_id, "pgh_context_id")

    with pghistory.context(hello="world"):
        denorm_model.int_field += 1
        denorm_model.save()

    assert denorm_model.event.count() == 2
    event = denorm_model.event.order_by("pgh_id").last()
    assert event.pgh_context == {"hello": "world"}
    assert isinstance(event.pgh_context_id, uuid.UUID)

    event_no_id = denorm_model.event_no_id.order_by("pgh_id").last()
    assert event_no_id.pgh_context == {"hello": "world"}


@pytest.mark.django_db
def test_unique_field_tracking():
    """Verifies tracking works on models with unique constraints"""
    pk_model = ddf.G(test_models.CustomModel)
    unique_model = ddf.G(
        test_models.UniqueConstraintModel,
        my_one_to_one=pk_model,
        my_char_field="1",
        my_int_field1=1,
        my_int_field2=2,
    )
    unique_model.my_int_field2 = 1
    unique_model.save()
    unique_model.my_int_field2 = 2
    unique_model.save()
    assert unique_model.snapshot.count() == 3


@pytest.mark.django_db
def test_m2m_through_tracking():
    """Verify we track events when users are added/removed from groups"""
    user = ddf.G("auth.User")
    g1 = ddf.G("auth.Group")
    g2 = ddf.G("auth.Group")

    assert not test_models.UserGroupsEvent.objects.exists()

    user.groups.add(g1)
    assert test_models.UserGroupsEvent.objects.count() == 1
    assert list(
        test_models.UserGroupsEvent.objects.values("user", "pgh_label", "group").order_by("pgh_id")
    ) == [{"user": user.id, "group": g1.id, "pgh_label": "group.add"}]

    user.groups.remove(g1)
    assert test_models.UserGroupsEvent.objects.count() == 2
    assert list(
        test_models.UserGroupsEvent.objects.values("user", "pgh_label", "group").order_by("pgh_id")
    ) == [
        {"user": user.id, "group": g1.id, "pgh_label": "group.add"},
        {"user": user.id, "group": g1.id, "pgh_label": "group.remove"},
    ]

    user.groups.add(g2)
    assert test_models.UserGroupsEvent.objects.count() == 3
    assert list(
        test_models.UserGroupsEvent.objects.values("user", "pgh_label", "group").order_by("pgh_id")
    ) == [
        {"user": user.id, "group": g1.id, "pgh_label": "group.add"},
        {"user": user.id, "group": g1.id, "pgh_label": "group.remove"},
        {"user": user.id, "group": g2.id, "pgh_label": "group.add"},
    ]


@pytest.mark.django_db
def test_custom_pk_and_custom_column():
    """
    Tests history tracking on a model with a custom primary key
    and custom column name
    """
    m = ddf.G("tests.CustomModel", int_field=1)
    m.int_field = 2
    m.save()

    assert m.snapshot.count() == 2
    assert list(m.snapshot.values_list("pgh_obj_id", flat=True).distinct()) == [m.pk]

    assert m.event.count() == 1
    assert m.event.get().int_field == 2


@pytest.mark.django_db
def test_create_event():
    """
    Verifies events can be created manually and are linked with proper
    context
    """
    m = ddf.G("tests.EventModel")
    with pytest.raises(ValueError, match="not a registered tracker"):
        pghistory.create_event(m, label="invalid_event")

    event = pghistory.create_event(m, label="manual_event")
    assert event.pgh_label == "manual_event"
    assert event.dt_field == m.dt_field
    assert event.int_field == m.int_field
    assert event.pgh_context is None

    event = pghistory.create_event(m, label="no_pgh_obj_manual_event")
    assert event.pgh_label == "no_pgh_obj_manual_event"
    assert event.dt_field == m.dt_field
    assert event.int_field == m.int_field
    assert event.pgh_context is None

    # Context should be added properly
    with pghistory.context(hello="world") as ctx:
        event = pghistory.create_event(m, label="manual_event")
        assert event.pgh_label == "manual_event"
        assert event.dt_field == m.dt_field
        assert event.int_field == m.int_field
        assert event.pgh_context.id == ctx.id
        assert event.pgh_context.metadata == {"hello": "world"}


@pytest.mark.django_db
def test_events_on_event_model(mocker):
    """
    Verifies events are created properly for EventModel
    """
    m = ddf.G("tests.EventModel")
    orig_dt = m.dt_field
    orig_int = m.int_field

    assert list(m.event.values()) == [
        {
            "pgh_operation": pghistory.utils.Operation.INSERT.value,
            "pgh_created_at": mocker.ANY,
            "dt_field": orig_dt,
            "pgh_id": mocker.ANY,
            "pgh_label": "model.create",
            "int_field": orig_int,
            "pgh_obj_id": m.id,
            "pgh_context_id": None,
            "id": m.id,
        }
    ]

    # A "before_update" will always fire, event if values
    # don't change
    m.save()
    assert list(m.event.values().order_by("pgh_id")) == [
        {
            "pgh_operation": pghistory.utils.Operation.INSERT.value,
            "pgh_created_at": mocker.ANY,
            "dt_field": orig_dt,
            "pgh_id": mocker.ANY,
            "pgh_label": "model.create",
            "int_field": orig_int,
            "pgh_obj_id": m.id,
            "pgh_context_id": None,
            "id": m.id,
        },
        {
            "pgh_operation": pghistory.utils.Operation.UPDATE.value,
            "pgh_created_at": mocker.ANY,
            "dt_field": orig_dt,
            "pgh_id": mocker.ANY,
            "pgh_label": "before_update",
            "int_field": orig_int,
            "pgh_obj_id": m.id,
            "pgh_context_id": None,
            "id": m.id,
        },
    ]

    # An "after_update" will fire when the dt_field
    # changes
    m.dt_field = dt.datetime(2019, 1, 1, tzinfo=dt.timezone.utc)
    m.save()
    assert list(m.event.values().order_by("pgh_id")) == [
        {
            "pgh_operation": pghistory.utils.Operation.INSERT.value,
            "pgh_created_at": mocker.ANY,
            "dt_field": orig_dt,
            "pgh_id": mocker.ANY,
            "pgh_label": "model.create",
            "int_field": orig_int,
            "pgh_obj_id": m.id,
            "pgh_context_id": None,
            "id": m.id,
        },
        {
            "pgh_operation": pghistory.utils.Operation.UPDATE.value,
            "pgh_created_at": mocker.ANY,
            "dt_field": orig_dt,
            "pgh_id": mocker.ANY,
            "pgh_label": "before_update",
            "int_field": orig_int,
            "pgh_obj_id": m.id,
            "pgh_context_id": None,
            "id": m.id,
        },
        {
            "pgh_operation": pghistory.utils.Operation.UPDATE.value,
            "pgh_created_at": mocker.ANY,
            "dt_field": m.dt_field,
            "pgh_id": mocker.ANY,
            "pgh_label": "after_update",
            "int_field": orig_int,
            "pgh_obj_id": m.id,
            "pgh_context_id": None,
            "id": m.id,
        },
        {
            "pgh_operation": pghistory.utils.Operation.UPDATE.value,
            "pgh_created_at": mocker.ANY,
            "dt_field": orig_dt,
            "pgh_id": mocker.ANY,
            "pgh_label": "before_update",
            "int_field": orig_int,
            "pgh_obj_id": m.id,
            "pgh_context_id": None,
            "id": m.id,
        },
    ]

    # Verify the custom event model was also created for every insert
    assert list(m.custom_related_name.values().order_by("pgh_id")) == [
        {
            "pgh_operation": pghistory.utils.Operation.INSERT.value,
            "pgh_created_at": mocker.ANY,
            "dt_field": orig_dt,
            "pgh_id": mocker.ANY,
            "pgh_label": "model.custom_create",
            "pgh_obj_id": m.id,
        }
    ]

    # Verify a "before_delete" is fired
    m_id = m.id
    dt_field = m.dt_field
    m.delete()
    assert list(
        test_models.EventModelEvent.objects.filter(pgh_label="before_delete").values()
    ) == [
        {
            "pgh_operation": pghistory.utils.Operation.DELETE.value,
            "pgh_created_at": mocker.ANY,
            "dt_field": dt_field,
            "pgh_id": mocker.ANY,
            "pgh_label": "before_delete",
            "int_field": orig_int,
            "pgh_obj_id": m_id,
            "pgh_context_id": None,
            "id": m_id,
        },
    ]


@pytest.mark.django_db
def test_dt_field_snapshot_tracking(mocker):
    """
    Tests the snapshot trigger for the dt_field tracker.
    """
    tracking = ddf.G(
        test_models.SnapshotModel,
        dt_field=dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc),
    )
    assert tracking.dt_field_snapshot.exists()

    tracking.dt_field = dt.datetime(2019, 1, 1, tzinfo=dt.timezone.utc)
    tracking.save()

    # Do an empty update to make sure extra snapshot aren't tracked
    tracking.save()

    assert list(tracking.dt_field_snapshot.order_by("pgh_id").values()) == [
        {
            "pgh_id": mocker.ANY,
            "pgh_label": "dt_field_snapshot",
            "dt_field": dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc),
            "pgh_obj_id": tracking.id,
            "pgh_created_at": mocker.ANY,
            "pgh_context_id": None,
            "pgh_operation": pghistory.utils.Operation.INSERT.value,
        },
        {
            "pgh_id": mocker.ANY,
            "pgh_label": "dt_field_snapshot",
            "dt_field": dt.datetime(2019, 1, 1, tzinfo=dt.timezone.utc),
            "pgh_obj_id": tracking.id,
            "pgh_created_at": mocker.ANY,
            "pgh_context_id": None,
            "pgh_operation": pghistory.utils.Operation.UPDATE.value,
        },
    ]


@pytest.mark.django_db
def test_dt_field_int_field_snapshot_tracking(mocker):
    """
    Tests the snapshot trigger for combinations of dt_field/int_field.
    """
    tracking = ddf.G(
        test_models.SnapshotModel,
        dt_field=dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc),
        int_field=0,
    )
    assert tracking.dt_field_int_field_snapshot.exists()

    tracking.dt_field = dt.datetime(2019, 1, 1, tzinfo=dt.timezone.utc)
    tracking.save()

    # Do an empty update to make sure extra snapshot aren't tracked
    tracking.save()

    # Update the int field
    tracking.int_field = 1
    tracking.save()

    assert list(tracking.dt_field_int_field_snapshot.order_by("pgh_id").values()) == [
        {
            "pgh_id": mocker.ANY,
            "dt_field": dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc),
            "pgh_label": "dt_field_int_field_snapshot",
            "int_field": 0,
            "pgh_obj_id": tracking.id,
            "pgh_created_at": mocker.ANY,
            "pgh_context_id": None,
            "pgh_operation": pghistory.utils.Operation.INSERT.value,
        },
        {
            "pgh_id": mocker.ANY,
            "dt_field": dt.datetime(2019, 1, 1, tzinfo=dt.timezone.utc),
            "pgh_label": "dt_field_int_field_snapshot",
            "int_field": 0,
            "pgh_obj_id": tracking.id,
            "pgh_created_at": mocker.ANY,
            "pgh_context_id": None,
            "pgh_operation": pghistory.utils.Operation.UPDATE.value,
        },
        {
            "pgh_id": mocker.ANY,
            "dt_field": dt.datetime(2019, 1, 1, tzinfo=dt.timezone.utc),
            "pgh_label": "dt_field_int_field_snapshot",
            "int_field": 1,
            "pgh_obj_id": tracking.id,
            "pgh_created_at": mocker.ANY,
            "pgh_context_id": None,
            "pgh_operation": pghistory.utils.Operation.UPDATE.value,
        },
    ]


@pytest.mark.django_db
def test_fk_cascading(mocker):
    """
    Makes a snapshot and then removes a foreign key. Since django
    will set this foreign key to null with a cascading operation, the
    history tracking should also capture this and preserve the original
    foreign key value.
    """
    orig_user = ddf.G("auth.User")
    tracking = ddf.G(test_models.SnapshotModel, fk_field=orig_user)
    orig_user_id = orig_user.id

    assert orig_user is not None
    assert list(tracking.snapshot.order_by("pgh_id").values("fk_field_id", "pgh_obj_id")) == [
        {"fk_field_id": tracking.fk_field_id, "pgh_obj_id": tracking.id}
    ]
    assert list(
        tracking.custom_related_name.order_by("pgh_id").values("fk_field_id", "pgh_obj_id")
    ) == [{"fk_field_id": tracking.fk_field_id, "pgh_obj_id": tracking.id}]
    original_custom_pgh_id = tracking.custom_related_name.get().pk

    # Deleting the user should set the user to None in the tracking model
    orig_user.delete()
    tracking.refresh_from_db()
    assert tracking.fk_field_id is None
    # The tracked history should retain the original user
    assert list(tracking.snapshot.order_by("pgh_id").values("fk_field_id", "pgh_obj_id")) == [
        {"fk_field_id": orig_user_id, "pgh_obj_id": tracking.id},
        {"fk_field_id": None, "pgh_obj_id": tracking.id},
    ]

    # The custom tracking model is set to cascade delete whenever users
    # are deleted. The original tracking row should be gone
    assert not tracking.custom_related_name.filter(pk=original_custom_pgh_id).exists()

    # A new tracking row is still created for the new SnapshotModel that has
    # its user value set to None because of the cascade
    assert list(
        tracking.custom_related_name.order_by("pgh_id").values("fk_field_id", "pgh_obj_id")
    ) == [{"fk_field_id": None, "pgh_obj_id": tracking.id}]


@pytest.mark.django_db
def test_model_snapshot_tracking(mocker):
    """
    Tests the snapshot trigger for any model snapshot
    """
    # Even though the context is only set for this section of code,
    # it actually persists through the whole transaction (if there is
    # one). Since tests are ran in a transaction by default, all
    # subsequent events will be grouped under the same context
    with pghistory.context() as ctx:
        tracking = ddf.G(
            test_models.SnapshotModel,
            dt_field=dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc),
            int_field=0,
            fk_field=ddf.F(),
        )
        assert tracking.snapshot.exists()

    tracking.dt_field = dt.datetime(2019, 1, 1, tzinfo=dt.timezone.utc)
    tracking.save()

    # Do an empty update to make sure extra snapshot aren't tracked
    tracking.save()

    # Update the int field
    tracking.int_field = 1
    tracking.save()

    assert list(tracking.snapshot.order_by("pgh_id").values()) == [
        {
            "pgh_id": mocker.ANY,
            "id": tracking.id,
            "fk_field_id": tracking.fk_field_id,
            "dt_field": dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc),
            "pgh_label": "snapshot",
            "int_field": 0,
            "pgh_obj_id": tracking.id,
            "pgh_created_at": mocker.ANY,
            "pgh_context_id": ctx.id,
            "pgh_operation": pghistory.utils.Operation.INSERT.value,
        },
        {
            "pgh_id": mocker.ANY,
            "id": tracking.id,
            "dt_field": dt.datetime(2019, 1, 1, tzinfo=dt.timezone.utc),
            "fk_field_id": tracking.fk_field_id,
            "pgh_label": "snapshot",
            "int_field": 0,
            "pgh_obj_id": tracking.id,
            "pgh_created_at": mocker.ANY,
            "pgh_context_id": ctx.id,
            "pgh_operation": pghistory.utils.Operation.UPDATE.value,
        },
        {
            "pgh_id": mocker.ANY,
            "id": tracking.id,
            "dt_field": dt.datetime(2019, 1, 1, tzinfo=dt.timezone.utc),
            "fk_field_id": tracking.fk_field_id,
            "pgh_label": "snapshot",
            "int_field": 1,
            "pgh_obj_id": tracking.id,
            "pgh_created_at": mocker.ANY,
            "pgh_context_id": ctx.id,
            "pgh_operation": pghistory.utils.Operation.UPDATE.value,
        },
    ]

    # Deleting the model will not delete history by default
    tracking.delete()
    assert apps.get_model("tests", "SnapshotModelSnapshot").objects.count() == 4


@pytest.mark.django_db
def test_custom_snapshot_model_tracking(mocker):
    """
    Tests the snapshot trigger when a custom snapshot model is declared
    """
    # There is no context foreign key, so no context should be saved
    with pghistory.context():
        tracking = ddf.G(test_models.SnapshotModel, int_field=0)
        assert tracking.custom_related_name.exists()

        tracking.int_field = 1
        tracking.save()

    # Do an empty update to make sure extra snapshot aren't tracked
    tracking.save()

    assert list(tracking.custom_related_name.order_by("pgh_id").values()) == [
        {
            "pgh_id": mocker.ANY,
            "id": tracking.id,
            "pgh_label": "custom_snapshot",
            "int_field": 0,
            "fk_field_id": tracking.fk_field_id,
            "fk_field2_id": None,
            "pgh_obj_id": tracking.id,
            "pgh_created_at": mocker.ANY,
            "pgh_operation": pghistory.utils.Operation.INSERT.value,
        },
        {
            "pgh_id": mocker.ANY,
            "id": tracking.id,
            "pgh_label": "custom_snapshot",
            "int_field": 1,
            "fk_field_id": tracking.fk_field_id,
            "fk_field2_id": None,
            "pgh_obj_id": tracking.id,
            "pgh_created_at": mocker.ANY,
            "pgh_operation": pghistory.utils.Operation.UPDATE.value,
        },
    ]
    assert list(tracking.custom_related_name.order_by("pgh_id").values()) == list(
        test_models.CustomSnapshotModel.objects.order_by("pgh_id").values()
    )

    # Deleting the model will not delete the tracking model since it
    # has a custom foreign key, but will generate a new snapshot
    assert test_models.CustomSnapshotModel.objects.count() == 2
    tracking.delete()
    assert test_models.CustomSnapshotModel.objects.count() == 3


@pytest.mark.parametrize(
    "val, expected_output",
    [("", ""), ("hello_world", "HelloWorld"), ("Hello", "Hello")],
)
def test_pascalcase(val, expected_output):
    assert pghistory.core._pascalcase(val) == expected_output


@pytest.mark.parametrize(
    "model_name, obj_fk, fields, expected_model_name, expected_related_name",
    [
        (None, pghistory.constants.UNSET, None, "EventModelEvent", "event"),
        (
            None,
            models.ForeignKey(
                "tests.EventModelEvent",
                on_delete=models.CASCADE,
                related_name="r",
            ),
            None,
            "EventModelEvent",
            "r",
        ),
        ("Name", pghistory.constants.UNSET, None, "Name", "event"),
        (
            None,
            pghistory.constants.UNSET,
            ["int_field"],
            "EventModelIntFieldEvent",
            "int_field_event",
        ),
        (
            None,
            pghistory.constants.UNSET,
            ["int_field", "dt_field"],
            "EventModelIntFieldDtFieldEvent",
            "int_field_dt_field_event",
        ),
    ],
)
def test_factory(model_name, obj_fk, fields, expected_model_name, expected_related_name):
    cls = pghistory.core.create_event_model(
        test_models.EventModel, model_name=model_name, obj_fk=obj_fk, fields=fields
    )

    assert cls.__name__ == expected_model_name
    assert cls._meta.get_field("pgh_obj").remote_field.related_name == expected_related_name


@pytest.mark.parametrize(
    "app_label, model_name, abstract, expected_exception",
    [
        ("tests", "Valid", False, no_exception()),
        (
            "tests",
            "CustomModel",
            False,
            pytest.raises(ValueError, match="already has"),
        ),
        ("tests", "CustomModel", True, no_exception()),
        (
            "invalid",
            "CustomModel",
            False,
            pytest.raises(ValueError, match="is invalid"),
        ),
        (
            "auth",
            "CustomModel",
            False,
            pytest.raises(ValueError, match="under third"),
        ),
    ],
)
def test_validate_event_model_path(app_label, model_name, abstract, expected_exception):
    """Tests pghistory.models._validate_event_model_path"""
    with expected_exception:
        pghistory.core._validate_event_model_path(
            app_label=app_label, model_name=model_name, abstract=abstract
        )


@pytest.mark.django_db
def test_custom_ignore_auto_fields_tracker():
    """
    Verifies that the custom IgnoreAutoFieldsSnapshot tracker, which makes use of
    pghistory.Changed, works.
    """
    m = ddf.G(test_models.IgnoreAutoFieldsSnapshotModel, my_int_field=0, my_char_field="0")
    snapshot_model = m.no_auto_fields_event.model

    # This tracker does not create events on insert
    assert not m.no_auto_fields_event.all()

    # Empty updates will not produce an event
    m.save()
    assert not m.no_auto_fields_event.all()

    # Updating a non-auto field will produce an event based on the OLD row
    m.my_int_field = 1
    m.save()
    assert [m.my_int_field for m in m.no_auto_fields_event.all()] == [0]

    # Update auto-fields manually. Make sure they don't product an event
    now = timezone.now()
    test_models.IgnoreAutoFieldsSnapshotModel.objects.update(created_at=now, updated_at=now)
    m.refresh_from_db()
    assert m.created_at == now
    assert m.updated_at == now
    assert snapshot_model.objects.count() == 1

    # Deleting the model will create another snapshot
    m.delete()

    assert snapshot_model.objects.count() == 2
