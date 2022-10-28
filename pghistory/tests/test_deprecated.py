import datetime as dt

import ddf
import pytest

import pghistory.models
import pghistory.tests.models as test_models
import pghistory.utils


@pytest.mark.django_db
def test_aggregate_event_default_manager():
    """Verifies the default manager for aggregate events returns no results"""
    assert list(pghistory.models.AggregateEvent.no_objects.all()) == []


@pytest.mark.django_db
def test_aggregate_events_no_history():
    """
    Tests the AggregateEvent proxy on a model that has no history tracking
    """
    untracked = ddf.G(test_models.UntrackedModel)
    assert list(pghistory.models.AggregateEvent.objects.target(untracked).all()) == []


@pytest.mark.django_db(transaction=True)
def test_aggregate_events_joining_filtering(django_assert_num_queries, mocker):
    """
    Test joining and other filtering for the AggregateEvent proxy.
    Use the CustomAggregateEvent subclass to verify we can filter/join on
    context metadata
    """
    actor = ddf.G("auth.User")
    # Create an event trail under various contexts
    with pghistory.context(key="value1", user=actor.id):
        user1 = ddf.G("auth.User")
        user2 = ddf.G("auth.User")
        sm1 = ddf.G(
            test_models.SnapshotModel,
            dt_field=dt.datetime(2020, 6, 17, tzinfo=dt.timezone.utc),
            int_field=1,
            fk_field=user1,
        )
        sm2 = ddf.G(
            test_models.SnapshotModel,
            dt_field=dt.datetime(2020, 6, 22, tzinfo=dt.timezone.utc),
            int_field=10,
            fk_field=user2,
        )

    with pghistory.context(key="value2", url="https://url.com", user=0):
        sm1.int_field = 2
        sm1.save()
        sm2.int_field = 22
        sm2.save()

    with pghistory.context(key="value3", user=actor.id):
        sm1.dt_field = dt.datetime(2020, 6, 19, tzinfo=dt.timezone.utc)
        sm1.int_field = 3
        sm1.save()
        sm2.int_field = 33
        sm2.save()
        sm2.fk_field = user1
        sm2.save()

    # Make sure we can join against our proxy model without performance issues
    with django_assert_num_queries(1):
        assert {
            e.pgh_context.metadata["key"]
            for e in pghistory.models.AggregateEvent.objects.target(sm1)
            .filter(pgh_context__isnull=False)
            .select_related("pgh_context")
        } == {"value1", "value2", "value3"}

    assert list(
        pghistory.models.AggregateEvent.objects.target(user1)
        .filter(pgh_label="snapshot", pgh_data__int_field=3)
        .values()
    ) == [
        {
            "pgh_context_id": mocker.ANY,
            "pgh_operation": pghistory.utils.Operation.UPDATE.value,
            "pgh_created_at": mocker.ANY,
            "pgh_data": {
                "dt_field": "2020-06-19T00:00:00+00:00",
                "fk_field_id": user1.id,
                "id": sm1.id,
                "int_field": 3,
            },
            "pgh_diff": {
                "dt_field": [
                    "2020-06-17T00:00:00+00:00",
                    "2020-06-19T00:00:00+00:00",
                ],
                "int_field": [2, 3],
            },
            "pgh_id": mocker.ANY,
            "pgh_label": "snapshot",
            "pgh_table": "tests_snapshotmodelsnapshot",
        }
    ]

    # Use the CustomAggregateEvent proxy to join on metadata fields.
    # In this case, we join the email of the user in the metadata.
    # Since we provided an invalid user for an event, "None" is returned
    assert set(
        test_models.CustomAggregateEvent.objects.target(user1)
        .values_list("user__email", flat=True)
        .distinct()
    ) == {actor.email, None}

    assert set(
        test_models.CustomAggregateEvent.objects.target(user1)
        .values_list("url", flat=True)
        .distinct()
    ) == {"https://url.com", None}


@pytest.mark.django_db(transaction=True)
def test_aggregate_events_joining_filtering_multiple_targets(django_assert_num_queries, mocker):
    """
    Test joining and other filtering for the AggregateEvent proxy with multiple targets.
    """
    user1 = ddf.G("auth.User")
    user2 = ddf.G("auth.User")
    sm1 = ddf.G(
        test_models.SnapshotModel,
        dt_field=dt.datetime(2020, 6, 17, tzinfo=dt.timezone.utc),
        int_field=1,
        fk_field=user1,
    )
    sm2 = ddf.G(
        test_models.SnapshotModel,
        dt_field=dt.datetime(2020, 6, 22, tzinfo=dt.timezone.utc),
        int_field=10,
        fk_field=user2,
    )

    sm1.int_field = 3
    sm1.save()
    sm2.int_field = 33
    sm2.fk_field = user1
    sm2.save()

    default = {
        "pgh_context_id": None,
        "pgh_operation": pghistory.utils.Operation.INSERT.value,
        "pgh_created_at": mocker.ANY,
        "pgh_id": mocker.ANY,
        "pgh_label": "snapshot",
        "pgh_table": "tests_snapshotmodelsnapshot",
    }
    wanted_result = [
        {
            **default,
            "pgh_data": {
                "dt_field": "2020-06-17T00:00:00+00:00",
                "fk_field_id": user1.id,
                "id": sm1.id,
                "int_field": 1,
            },
            "pgh_diff": None,
        },
        {
            **default,
            "pgh_data": {
                "dt_field": "2020-06-22T00:00:00+00:00",
                "fk_field_id": user2.id,
                "id": sm2.id,
                "int_field": 10,
            },
            "pgh_diff": None,
        },
        {
            **default,
            "pgh_operation": pghistory.utils.Operation.UPDATE.value,
            "pgh_data": {
                "dt_field": "2020-06-17T00:00:00+00:00",
                "fk_field_id": user1.id,
                "id": sm1.id,
                "int_field": 3,
            },
            "pgh_diff": {
                "int_field": [1, 3],
            },
        },
        {
            **default,
            "pgh_operation": pghistory.utils.Operation.UPDATE.value,
            "pgh_data": {
                "dt_field": "2020-06-22T00:00:00+00:00",
                "fk_field_id": user1.id,
                "id": sm2.id,
                "int_field": 33,
            },
            "pgh_diff": {
                "int_field": [10, 33],
                "fk_field_id": [user2.id, user1.id],
            },
        },
    ]

    assert (
        list(
            pghistory.models.AggregateEvent.objects.target([sm1, sm2])
            .filter(pgh_label="snapshot")
            .order_by("pgh_created_at")
            .values()
        )
        == wanted_result
    )

    assert (
        list(
            pghistory.models.AggregateEvent.objects.target(test_models.SnapshotModel.objects.all())
            .filter(pgh_label="snapshot")
            .order_by("pgh_created_at")
            .values()
        )
        == wanted_result
    )


@pytest.mark.django_db
def test_aggregate_events_custom_pk(mocker):
    """
    Verify that the AggregateEvent proxy model properly aggregates
    events across models with custom PKs
    """
    cm = ddf.G(test_models.CustomModel, int_field=1)
    cm.int_field = 2
    cm.save()
    cm.save()

    assert list(
        pghistory.models.AggregateEvent.objects.target(cm).order_by("pgh_table", "pgh_id").values()
    ) == [
        {
            "pgh_context_id": None,
            "pgh_operation": pghistory.utils.Operation.UPDATE.value,
            "pgh_created_at": mocker.ANY,
            "pgh_data": {"integer_field": 2, "my_pk": str(cm.pk)},
            "pgh_diff": None,
            "pgh_id": mocker.ANY,
            "pgh_label": "int_field_updated",
            "pgh_table": "tests_custommodelevent",
        },
        {
            "pgh_context_id": None,
            "pgh_operation": pghistory.utils.Operation.INSERT.value,
            "pgh_created_at": mocker.ANY,
            "pgh_data": {"integer_field": 1, "my_pk": str(cm.pk)},
            "pgh_diff": None,
            "pgh_id": mocker.ANY,
            "pgh_label": "snapshot",
            "pgh_table": "tests_custommodelsnapshot",
        },
        {
            "pgh_context_id": None,
            "pgh_operation": pghistory.utils.Operation.UPDATE.value,
            "pgh_created_at": mocker.ANY,
            "pgh_data": {"integer_field": 2, "my_pk": str(cm.pk)},
            "pgh_diff": {"integer_field": [1, 2]},
            "pgh_id": mocker.ANY,
            "pgh_label": "snapshot",
            "pgh_table": "tests_custommodelsnapshot",
        },
    ]


@pytest.mark.django_db
def test_aggregate_events_usage():
    """Verifies the AggregateEvent queryset is used properly"""
    with pytest.raises(ValueError, match=".target()"):
        list(pghistory.models.AggregateEvent.objects.all())

    with pghistory.context(hello="a"):
        cm = ddf.G(test_models.CustomModel, int_field=1)

    with pytest.raises(ValueError, match="does not reference"):
        list(
            pghistory.models.AggregateEvent.objects.target(cm).across(
                test_models.CustomSnapshotModel
            )
        )


@pytest.mark.django_db
def test_aggregate_events_no_obj_tracking_filters(mocker):
    """
    Verify that the AggregateEvent proxy model properly aggregates
    events even when the event models have no pgh_obj reference
    """
    user1 = ddf.G("auth.User")
    user2 = ddf.G("auth.User")
    sm1 = ddf.G(
        test_models.SnapshotModel,
        dt_field=dt.datetime(2020, 6, 17, tzinfo=dt.timezone.utc),
        int_field=1,
        fk_field=user1,
    )
    sm1.int_field = 2
    sm1.save()
    sm1.dt_field = dt.datetime(2020, 6, 19, tzinfo=dt.timezone.utc)
    sm1.int_field = 3
    sm1.save()

    sm2 = ddf.G(
        test_models.SnapshotModel,
        dt_field=dt.datetime(2020, 6, 22, tzinfo=dt.timezone.utc),
        int_field=10,
        fk_field=user2,
    )
    sm2.int_field = 22
    sm2.save()
    sm2.int_field = 33
    sm2.save()
    sm2.fk_field = user1
    sm2.save()

    assert list(
        pghistory.models.AggregateEvent.objects.target(sm1)
        .order_by("pgh_table", "pgh_id")
        .values()
    ) == [
        {
            "pgh_context_id": None,
            "pgh_operation": pghistory.utils.Operation.INSERT.value,
            "pgh_created_at": mocker.ANY,
            "pgh_data": {
                "fk_field2_id": None,
                "fk_field_id": user1.id,
                "id": sm1.id,
                "int_field": 1,
            },
            "pgh_diff": None,
            "pgh_id": mocker.ANY,
            "pgh_label": "custom_snapshot",
            "pgh_table": "tests_customsnapshotmodel",
        },
        {
            "pgh_context_id": None,
            "pgh_operation": pghistory.utils.Operation.UPDATE.value,
            "pgh_created_at": mocker.ANY,
            "pgh_data": {
                "fk_field2_id": None,
                "fk_field_id": user1.id,
                "id": sm1.id,
                "int_field": 2,
            },
            "pgh_diff": {"int_field": [1, 2]},
            "pgh_id": mocker.ANY,
            "pgh_label": "custom_snapshot",
            "pgh_table": "tests_customsnapshotmodel",
        },
        {
            "pgh_context_id": None,
            "pgh_operation": pghistory.utils.Operation.UPDATE.value,
            "pgh_created_at": mocker.ANY,
            "pgh_data": {
                "fk_field2_id": None,
                "fk_field_id": user1.id,
                "id": sm1.id,
                "int_field": 3,
            },
            "pgh_diff": {"int_field": [2, 3]},
            "pgh_id": mocker.ANY,
            "pgh_label": "custom_snapshot",
            "pgh_table": "tests_customsnapshotmodel",
        },
        {
            "pgh_context_id": None,
            "pgh_operation": pghistory.utils.Operation.INSERT.value,
            "pgh_created_at": mocker.ANY,
            "pgh_data": {"dt_field": "2020-06-17T00:00:00+00:00"},
            "pgh_diff": None,
            "pgh_id": mocker.ANY,
            "pgh_label": "dt_field_snapshot",
            "pgh_table": "tests_snapshotmodeldtfieldevent",
        },
        {
            "pgh_context_id": None,
            "pgh_operation": pghistory.utils.Operation.UPDATE.value,
            "pgh_created_at": mocker.ANY,
            "pgh_data": {"dt_field": "2020-06-19T00:00:00+00:00"},
            "pgh_diff": {
                "dt_field": [
                    "2020-06-17T00:00:00+00:00",
                    "2020-06-19T00:00:00+00:00",
                ]
            },
            "pgh_id": mocker.ANY,
            "pgh_label": "dt_field_snapshot",
            "pgh_table": "tests_snapshotmodeldtfieldevent",
        },
        {
            "pgh_context_id": None,
            "pgh_operation": pghistory.utils.Operation.INSERT.value,
            "pgh_created_at": mocker.ANY,
            "pgh_data": {
                "dt_field": "2020-06-17T00:00:00+00:00",
                "int_field": 1,
            },
            "pgh_diff": None,
            "pgh_id": mocker.ANY,
            "pgh_label": "dt_field_int_field_snapshot",
            "pgh_table": "tests_snapshotmodeldtfieldintfieldevent",
        },
        {
            "pgh_context_id": None,
            "pgh_operation": pghistory.utils.Operation.UPDATE.value,
            "pgh_created_at": mocker.ANY,
            "pgh_data": {
                "dt_field": "2020-06-17T00:00:00+00:00",
                "int_field": 2,
            },
            "pgh_diff": {"int_field": [1, 2]},
            "pgh_id": mocker.ANY,
            "pgh_label": "dt_field_int_field_snapshot",
            "pgh_table": "tests_snapshotmodeldtfieldintfieldevent",
        },
        {
            "pgh_context_id": None,
            "pgh_operation": pghistory.utils.Operation.UPDATE.value,
            "pgh_created_at": mocker.ANY,
            "pgh_data": {
                "dt_field": "2020-06-19T00:00:00+00:00",
                "int_field": 3,
            },
            "pgh_diff": {
                "dt_field": [
                    "2020-06-17T00:00:00+00:00",
                    "2020-06-19T00:00:00+00:00",
                ],
                "int_field": [2, 3],
            },
            "pgh_id": mocker.ANY,
            "pgh_label": "dt_field_int_field_snapshot",
            "pgh_table": "tests_snapshotmodeldtfieldintfieldevent",
        },
        {
            "pgh_context_id": None,
            "pgh_operation": pghistory.utils.Operation.INSERT.value,
            "pgh_created_at": mocker.ANY,
            "pgh_data": {
                "dt_field": "2020-06-17T00:00:00+00:00",
                "fk_field_id": user1.id,
                "id": sm1.id,
                "int_field": 1,
            },
            "pgh_diff": None,
            "pgh_id": mocker.ANY,
            "pgh_label": "snapshot",
            "pgh_table": "tests_snapshotmodelsnapshot",
        },
        {
            "pgh_context_id": None,
            "pgh_operation": pghistory.utils.Operation.UPDATE.value,
            "pgh_created_at": mocker.ANY,
            "pgh_data": {
                "dt_field": "2020-06-17T00:00:00+00:00",
                "fk_field_id": user1.id,
                "id": sm1.id,
                "int_field": 2,
            },
            "pgh_diff": {"int_field": [1, 2]},
            "pgh_id": mocker.ANY,
            "pgh_label": "snapshot",
            "pgh_table": "tests_snapshotmodelsnapshot",
        },
        {
            "pgh_context_id": None,
            "pgh_operation": pghistory.utils.Operation.UPDATE.value,
            "pgh_created_at": mocker.ANY,
            "pgh_data": {
                "dt_field": "2020-06-19T00:00:00+00:00",
                "fk_field_id": user1.id,
                "id": sm1.id,
                "int_field": 3,
            },
            "pgh_diff": {
                "dt_field": [
                    "2020-06-17T00:00:00+00:00",
                    "2020-06-19T00:00:00+00:00",
                ],
                "int_field": [2, 3],
            },
            "pgh_id": mocker.ANY,
            "pgh_label": "snapshot",
            "pgh_table": "tests_snapshotmodelsnapshot",
        },
    ]

    # Check events on the user model, which will aggregate event tables
    # that have no pgh_obj. All events here will have a reference to user1
    assert list(
        pghistory.models.AggregateEvent.objects.target(user1)
        .order_by("pgh_table", "pgh_id")
        .values()
    ) == [
        {
            "pgh_context_id": None,
            "pgh_operation": pghistory.utils.Operation.INSERT.value,
            "pgh_created_at": mocker.ANY,
            "pgh_data": {
                "fk_field2_id": None,
                "fk_field_id": user1.id,
                "id": sm1.id,
                "int_field": 1,
            },
            "pgh_diff": None,
            "pgh_id": mocker.ANY,
            "pgh_label": "custom_snapshot",
            "pgh_table": "tests_customsnapshotmodel",
        },
        {
            "pgh_context_id": None,
            "pgh_operation": pghistory.utils.Operation.UPDATE.value,
            "pgh_created_at": mocker.ANY,
            "pgh_data": {
                "fk_field2_id": None,
                "fk_field_id": user1.id,
                "id": sm1.id,
                "int_field": 2,
            },
            "pgh_diff": {"int_field": [1, 2]},
            "pgh_id": mocker.ANY,
            "pgh_label": "custom_snapshot",
            "pgh_table": "tests_customsnapshotmodel",
        },
        {
            "pgh_context_id": None,
            "pgh_operation": pghistory.utils.Operation.UPDATE.value,
            "pgh_created_at": mocker.ANY,
            "pgh_data": {
                "fk_field2_id": None,
                "fk_field_id": user1.id,
                "id": sm1.id,
                "int_field": 3,
            },
            "pgh_diff": {"int_field": [2, 3]},
            "pgh_id": mocker.ANY,
            "pgh_label": "custom_snapshot",
            "pgh_table": "tests_customsnapshotmodel",
        },
        {
            "pgh_context_id": None,
            "pgh_operation": pghistory.utils.Operation.UPDATE.value,
            "pgh_created_at": mocker.ANY,
            "pgh_data": {
                "fk_field2_id": None,
                "fk_field_id": user1.id,
                "id": sm2.id,
                "int_field": 33,
            },
            "pgh_diff": None,
            "pgh_id": mocker.ANY,
            "pgh_label": "custom_snapshot",
            "pgh_table": "tests_customsnapshotmodel",
        },
        {
            "pgh_context_id": None,
            "pgh_operation": pghistory.utils.Operation.INSERT.value,
            "pgh_created_at": mocker.ANY,
            "pgh_data": {
                "dt_field": "2020-06-17T00:00:00+00:00",
                "fk_field_id": user1.id,
                "id": sm1.id,
                "int_field": 1,
            },
            "pgh_diff": None,
            "pgh_id": mocker.ANY,
            "pgh_label": "no_pgh_obj_snapshot",
            "pgh_table": "tests_nopghobjsnapshot",
        },
        {
            "pgh_context_id": None,
            "pgh_operation": pghistory.utils.Operation.UPDATE.value,
            "pgh_created_at": mocker.ANY,
            "pgh_data": {
                "dt_field": "2020-06-17T00:00:00+00:00",
                "fk_field_id": user1.id,
                "id": sm1.id,
                "int_field": 2,
            },
            "pgh_diff": None,
            "pgh_id": mocker.ANY,
            "pgh_label": "no_pgh_obj_snapshot",
            "pgh_table": "tests_nopghobjsnapshot",
        },
        {
            "pgh_context_id": None,
            "pgh_operation": pghistory.utils.Operation.UPDATE.value,
            "pgh_created_at": mocker.ANY,
            "pgh_data": {
                "dt_field": "2020-06-19T00:00:00+00:00",
                "fk_field_id": user1.id,
                "id": sm1.id,
                "int_field": 3,
            },
            "pgh_diff": None,
            "pgh_id": mocker.ANY,
            "pgh_label": "no_pgh_obj_snapshot",
            "pgh_table": "tests_nopghobjsnapshot",
        },
        {
            "pgh_context_id": None,
            "pgh_operation": pghistory.utils.Operation.UPDATE.value,
            "pgh_created_at": mocker.ANY,
            "pgh_data": {
                "dt_field": "2020-06-22T00:00:00+00:00",
                "fk_field_id": user1.id,
                "id": sm2.id,
                "int_field": 33,
            },
            "pgh_diff": None,
            "pgh_id": mocker.ANY,
            "pgh_label": "no_pgh_obj_snapshot",
            "pgh_table": "tests_nopghobjsnapshot",
        },
        {
            "pgh_context_id": None,
            "pgh_operation": pghistory.utils.Operation.INSERT.value,
            "pgh_created_at": mocker.ANY,
            "pgh_data": {
                "dt_field": "2020-06-17T00:00:00+00:00",
                "fk_field_id": user1.id,
                "id": sm1.id,
                "int_field": 1,
            },
            "pgh_diff": None,
            "pgh_id": mocker.ANY,
            "pgh_label": "snapshot",
            "pgh_table": "tests_snapshotmodelsnapshot",
        },
        {
            "pgh_context_id": None,
            "pgh_operation": pghistory.utils.Operation.UPDATE.value,
            "pgh_created_at": mocker.ANY,
            "pgh_data": {
                "dt_field": "2020-06-17T00:00:00+00:00",
                "fk_field_id": user1.id,
                "id": sm1.id,
                "int_field": 2,
            },
            "pgh_diff": {"int_field": [1, 2]},
            "pgh_id": mocker.ANY,
            "pgh_label": "snapshot",
            "pgh_table": "tests_snapshotmodelsnapshot",
        },
        {
            "pgh_context_id": None,
            "pgh_operation": pghistory.utils.Operation.UPDATE.value,
            "pgh_created_at": mocker.ANY,
            "pgh_data": {
                "dt_field": "2020-06-19T00:00:00+00:00",
                "fk_field_id": user1.id,
                "id": sm1.id,
                "int_field": 3,
            },
            "pgh_diff": {
                "dt_field": [
                    "2020-06-17T00:00:00+00:00",
                    "2020-06-19T00:00:00+00:00",
                ],
                "int_field": [2, 3],
            },
            "pgh_id": mocker.ANY,
            "pgh_label": "snapshot",
            "pgh_table": "tests_snapshotmodelsnapshot",
        },
        {
            "pgh_context_id": None,
            "pgh_operation": pghistory.utils.Operation.UPDATE.value,
            "pgh_created_at": mocker.ANY,
            "pgh_data": {
                "dt_field": "2020-06-22T00:00:00+00:00",
                "fk_field_id": user1.id,
                "id": sm2.id,
                "int_field": 33,
            },
            "pgh_diff": None,
            "pgh_id": mocker.ANY,
            "pgh_label": "snapshot",
            "pgh_table": "tests_snapshotmodelsnapshot",
        },
    ]

    # Only aggregate across some event models
    assert list(
        pghistory.models.AggregateEvent.objects.target(sm1)
        .across(test_models.CustomSnapshotModel)
        .order_by("pgh_table", "pgh_id")
        .values()
    ) == [
        {
            "pgh_context_id": None,
            "pgh_operation": pghistory.utils.Operation.INSERT.value,
            "pgh_created_at": mocker.ANY,
            "pgh_data": {
                "fk_field2_id": None,
                "fk_field_id": user1.id,
                "id": sm1.id,
                "int_field": 1,
            },
            "pgh_diff": None,
            "pgh_id": mocker.ANY,
            "pgh_label": "custom_snapshot",
            "pgh_table": "tests_customsnapshotmodel",
        },
        {
            "pgh_context_id": None,
            "pgh_operation": pghistory.utils.Operation.UPDATE.value,
            "pgh_created_at": mocker.ANY,
            "pgh_data": {
                "fk_field2_id": None,
                "fk_field_id": user1.id,
                "id": sm1.id,
                "int_field": 2,
            },
            "pgh_diff": {"int_field": [1, 2]},
            "pgh_id": mocker.ANY,
            "pgh_label": "custom_snapshot",
            "pgh_table": "tests_customsnapshotmodel",
        },
        {
            "pgh_context_id": None,
            "pgh_operation": pghistory.utils.Operation.UPDATE.value,
            "pgh_created_at": mocker.ANY,
            "pgh_data": {
                "fk_field2_id": None,
                "fk_field_id": user1.id,
                "id": sm1.id,
                "int_field": 3,
            },
            "pgh_diff": {"int_field": [2, 3]},
            "pgh_id": mocker.ANY,
            "pgh_label": "custom_snapshot",
            "pgh_table": "tests_customsnapshotmodel",
        },
    ]
