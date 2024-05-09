# Generated by Django 4.2.6 on 2023-10-15 13:28

import pgtrigger.compiler
import pgtrigger.migrations
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("tests", "0007_delete_customaggregateevent_and_more"),
    ]

    operations = [
        pgtrigger.migrations.AddTrigger(
            model_name="uniqueconstraintmodelevent",
            trigger=pgtrigger.compiler.Trigger(
                name="append_only",
                sql=pgtrigger.compiler.UpsertTriggerSql(
                    func="RAISE EXCEPTION 'pgtrigger: Cannot update or delete rows from % table', TG_TABLE_NAME;",  # noqa
                    hash="9046058a51f9b972e496ee43faf30cd0403f70e2",
                    operation="UPDATE OR DELETE",
                    pgid="pgtrigger_append_only_a73d3",
                    table="tests_uniqueconstraintmodelevent",
                    when="BEFORE",
                ),
            ),
        ),
    ]