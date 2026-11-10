"""Scoped Dashboard data and audit serialization."""

import json

from tests.test_scope_service import BOT, OTHER_BOT, OWNER, PRIMARY, REVIEWER, USER


async def test_scoped_dashboard_applications_and_grants(scoped):
    service, napcat = scoped
    napcat.set_member(PRIMARY, USER, "25")
    await service.apply(BOT, USER, "PRIVATE", "")
    app, = (await service.overview(BOT))["applications"]
    note = 'quotes "and", commas'
    await service.review(BOT, app["id"], REVIEWER, "REVIEWER", approve=True, note=note)
    view = await service.overview(BOT, include_audit=True)
    assert view["applications"][0]["status"] == "APPROVED"
    assert view["grants"][0]["scope"] == "PRIVATE"
    audit = json.loads(json.dumps(view))["audit"][0]
    assert audit["bot_self_id"] == BOT
    assert json.loads(audit["details_json"])["note"] == note
    assert not (await service.overview(OTHER_BOT, include_audit=True))["grants"]


async def test_scoped_policy_update_stays_on_verified_bot(scoped):
    service, _ = scoped
    await service.configure(BOT, OWNER, policies={"GROUP": {"min_level": 30}})
    assert (await service.overview(BOT))["policies"]["GROUP"]["min_level"] == 30
    assert (await service.overview(BOT))["policies"]["PRIVATE"]["min_level"] == 20
    defaults = (await service.overview(OTHER_BOT))["policies"]
    assert (defaults["GROUP"]["min_level"], defaults["PRIVATE"]["min_level"]) == (20, 50)
