"""Scoped Dashboard data and audit export serializers."""

import csv
import io
import json

from tests.test_scope_service import BOT, OTHER_BOT, OWNER, PRIMARY, REVIEWER, USER


async def test_scoped_dashboard_applications_and_grants(scoped):
    service, napcat = scoped
    napcat.set_member(PRIMARY, USER, "25")
    await service.apply(BOT, USER, "PRIVATE", "")
    app, = (await service.overview(BOT))["applications"]
    await service.review(BOT, app["id"], REVIEWER, "REVIEWER", approve=True)
    view = await service.overview(BOT, include_audit=True)
    assert view["applications"][0]["status"] == "APPROVED"
    assert view["grants"][0]["scope"] == "PRIVATE"
    assert view["audit"][0]["bot_self_id"] == BOT
    assert not (await service.overview(OTHER_BOT, include_audit=True))["grants"]


async def test_scoped_policy_update_stays_on_verified_bot(scoped):
    service, _ = scoped
    await service.configure(BOT, OWNER, policies={"GROUP": {"min_level": 30}})
    assert (await service.overview(BOT))["policies"]["GROUP"]["min_level"] == 30
    assert (await service.overview(OTHER_BOT))["policies"]["GROUP"]["min_level"] == 10


async def test_audit_json_and_csv_escape_fields(scoped):
    service, _ = scoped
    await service.audit.log(
        OWNER, "OWNER", BOT, PRIMARY, USER, "TEST_AUDIT",
        details={"note": 'quotes "and", commas'},
    )
    rows = json.loads(await service.audit.export_json(bot_self_id=BOT, action="TEST_AUDIT"))
    assert json.loads(rows[0]["details_json"])["note"] == 'quotes "and", commas'
    rows = list(csv.DictReader(io.StringIO(await service.audit.export_csv(bot_self_id=BOT, action="TEST_AUDIT"))))
    assert len(rows) == 1
    assert json.loads(rows[0]["details_json"])["note"] == 'quotes "and", commas'
