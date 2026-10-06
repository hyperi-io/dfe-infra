#  Project:      dfe-infra
#  File:         scripts/tests/test_onboarding.py
#  Purpose:      The onboarding suite's wizard plan and the access summary the
#                deploy hands its launcher, checked without a browser or a cluster
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What the onboarding run expects, and what a deploy hands over, on fixtures.

The two halves that decide whether a run is strict live away from the browser:
which screens a deployment must show, and where the password the run signs in
with comes from. Both are exercised here on documents written by hand, so the
suite's judgement is testable without a deployment to point it at.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import access_summary  # noqa: E402
from acceptance.onboarding import run as onboarding_run  # noqa: E402
from acceptance.onboarding import wizard  # noqa: E402


def _dfe_ops():
    """dfe-ops as a module: it is an extensionless CLI, so import it by path."""
    spec = importlib.util.spec_from_loader(
        "dfe_ops", importlib.machinery.SourceFileLoader("dfe_ops", str(SCRIPTS / "dfe-ops"))
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ops = _dfe_ops()

OLD_RELEASE = ("oidc_provider", "organisations", "first_user", "admin_password")
NEW_RELEASE = ("oidc_provider", "organisations", "first_user")


class TestWhichScreensAreExpected:
    def test_an_engine_that_still_declares_admin_password_expects_the_reset_screen(self):
        assert wizard.RESET_BREAK_GLASS in wizard.expected_slugs(OLD_RELEASE)

    def test_an_engine_that_dropped_it_does_not(self):
        assert wizard.RESET_BREAK_GLASS not in wizard.expected_slugs(NEW_RELEASE)

    def test_the_other_screens_are_the_same_either_way(self):
        old = [s for s in wizard.expected_slugs(OLD_RELEASE) if s != wizard.RESET_BREAK_GLASS]
        assert old == list(wizard.expected_slugs(NEW_RELEASE))

    def test_the_order_is_the_consoles_own(self):
        assert wizard.expected_slugs(OLD_RELEASE) == wizard.WIZARD_SLUGS

    def test_a_screen_the_deployment_does_not_ask_for_is_reported(self):
        expected = wizard.expected_slugs(NEW_RELEASE)
        seen = [wizard.WELCOME, wizard.ORGANISATION, wizard.RESET_BREAK_GLASS]

        assert wizard.unexpected_screens(expected, seen) == (wizard.RESET_BREAK_GLASS,)

    def test_a_run_that_met_only_expected_screens_reports_none(self):
        expected = wizard.expected_slugs(OLD_RELEASE)

        assert wizard.unexpected_screens(expected, expected) == ()

    def test_a_seeded_first_user_ends_the_wizard_at_the_organisation(self):
        expected = wizard.expected_slugs(NEW_RELEASE, pending=("oidc_provider", "organisations"))

        assert expected == (wizard.WELCOME, wizard.ORGANISATION)

    def test_every_step_pending_expects_every_screen(self):
        assert wizard.expected_slugs(NEW_RELEASE, pending=NEW_RELEASE) == wizard.expected_slugs(NEW_RELEASE)

    def test_a_reset_screen_already_done_is_not_expected_again(self):
        pending = ("oidc_provider", "organisations", "first_user")

        assert wizard.RESET_BREAK_GLASS not in wizard.expected_slugs(OLD_RELEASE, pending=pending)

    def test_pending_steps_come_off_the_document(self):
        status = {"initial_setup": {"steps": list(NEW_RELEASE), "pending_steps": ["organisations"]}}

        assert wizard.pending_steps(status) == ("organisations",)

    def test_a_document_without_pending_steps_treats_every_step_as_pending(self):
        status = {"initial_setup": {"steps": list(NEW_RELEASE), "complete": False}}

        assert wizard.pending_steps(status) == NEW_RELEASE


class TestReadingTheEngineContract:
    def test_the_steps_come_off_the_document(self):
        status = {"initial_setup": {"steps": list(OLD_RELEASE), "complete": False}}

        assert wizard.engine_steps(status) == OLD_RELEASE
        assert not wizard.setup_complete(status)

    def test_a_document_without_the_block_refuses_rather_than_guessing(self):
        with pytest.raises(wizard.OnboardingError, match="initial_setup"):
            wizard.engine_steps({"oidc_providers": []})

    def test_a_finished_deployment_says_so(self):
        assert wizard.setup_complete({"initial_setup": {"steps": [], "complete": True}})


class TestHowAFieldIsFound:
    # The console renders a required field's label with an asterisk beside it and
    # an optional one with a trailing space, so both forms have to match.
    def test_a_required_field_is_found_through_its_marker(self):
        assert onboarding_run._label("Username").match("Username *")

    def test_an_optional_fields_trailing_space_is_tolerated(self):
        assert onboarding_run._label("Name").match("Name ")

    def test_a_label_with_no_marker_still_matches(self):
        assert onboarding_run._label("Display Name").match("Display Name")

    def test_a_shorter_label_does_not_match_a_longer_one(self):
        assert not onboarding_run._label("Name").match("Username *")

    def test_the_source_name_field_is_found_on_either_console(self):
        # ui v1.7.0 renders "Source Name *"; the rc.13 console renders "Source".
        pattern = onboarding_run._label(*onboarding_run.SOURCE_NAME_LABELS)

        assert pattern.match("Source Name *")
        assert pattern.match("Source")

    def test_the_source_name_labels_match_no_neighbouring_field(self):
        pattern = onboarding_run._label(*onboarding_run.SOURCE_NAME_LABELS)

        for neighbour in ("Display Name", "Source Type *", "Name *", "Field *"):
            assert not pattern.match(neighbour), neighbour


class _Control:
    def __init__(self, page, label=""):
        self._page, self._label = page, label

    def count(self):
        return 1 if self._label in self._page.offered else 0

    def wait_for(self, **_):
        pass

    def fill(self, value):
        self._page.filled[self._label] = value

    def click(self, **_):
        self._page.clicked(self._label)

    def check(self, **_):
        self._page.checked.append(self._label)


class _Page:
    """A console that lands each password on a scripted URL, or keeps it on the form."""

    def __init__(self, ui, landings, after_change="/setup", offered=()):
        self.ui, self.landings, self.url, self.filled = ui, landings, f"{ui}/login", {}
        self.after_change, self.logins = after_change, []
        self.offered, self.checked = set(offered), []

    def goto(self, url, **_):
        self.url = url

    def get_by_role(self, _role, name="", **_):
        return _Control(self, name if isinstance(name, str) else "")

    def clicked(self, label):
        if label == "Login":
            self.logins.append(self.filled["Password"])
            self.url = f"{self.ui}{self.landings.get(self.filled['Password'], '/login')}"
        elif label == "Set password":
            self.url = f"{self.ui}{self.after_change}"

    def wait_for_url(self, predicate, **_):
        if not predicate(self.url):
            raise _StubTimeoutError()


class _StubTimeoutError(Exception):
    pass


class _Driver:
    def __init__(self, landings, after_change="/setup", offered=()):
        self.ui = "https://dfe.example"
        self.page = _Page(self.ui, landings, after_change, offered)
        self.records = []

    def textbox(self, *names):
        return _Control(self.page, names[0])

    def button(self, *names):
        return _Control(self.page, names[0])

    def record(self, *row):
        self.records.append(row)


class TestTheForcedChange:
    @pytest.fixture(autouse=True)
    def _no_browser(self, monkeypatch):
        stub = type(sys)("playwright.sync_api")
        stub.TimeoutError = _StubTimeoutError
        monkeypatch.setitem(sys.modules, "playwright", type(sys)("playwright"))
        monkeypatch.setitem(sys.modules, "playwright.sync_api", stub)

    def test_the_issued_password_leads_to_the_change(self):
        driver = _Driver({"issued": onboarding_run.CHANGE_PASSWORD_PATH})

        assert onboarding_run.sign_in_as_admin(driver, "admin", "issued", "chosen") == "chosen"
        assert driver.page.filled["Current Password"] == "issued"
        assert driver.page.filled["New Password"] == "chosen"

    def test_a_change_that_signs_the_admin_out_signs_back_in_with_the_new_password(self):
        driver = _Driver(
            {"issued": onboarding_run.CHANGE_PASSWORD_PATH, "chosen": "/setup"},
            after_change="/login",
        )

        assert onboarding_run.sign_in_as_admin(driver, "admin", "issued", "chosen") == "chosen"
        assert driver.page.logins == ["issued", "chosen"]
        assert driver.page.url.endswith("/setup")

    def test_an_issued_password_let_straight_in_fails_the_run(self):
        driver = _Driver({"issued": "/setup"})

        with pytest.raises(wizard.OnboardingError, match="without the forced change"):
            onboarding_run.sign_in_as_admin(driver, "admin", "issued", "chosen")

    def test_a_password_already_changed_signs_straight_in(self):
        driver = _Driver({"chosen": "/sources"})

        assert onboarding_run.sign_in_as_admin(driver, "admin", "issued", "chosen") == "chosen"

    def test_a_second_ui_run_signs_in_with_what_the_first_changed_the_admin_to(self):
        changed_to = ops.ui_admin_password({}, "minted")
        first = _Driver(
            {"minted": onboarding_run.CHANGE_PASSWORD_PATH, changed_to: "/setup"},
            after_change="/login",
        )
        assert onboarding_run.sign_in_as_admin(first, "admin", "minted", changed_to) == changed_to
        # The minted password is dead now, so only the one the first run chose opens the console.
        second = _Driver({changed_to: "/setup"})

        signed_in_with = onboarding_run.sign_in_as_admin(
            second, "admin", "minted", ops.ui_admin_password({}, "minted")
        )

        assert signed_in_with == changed_to
        assert second.page.logins == ["minted", changed_to]

    def test_after_the_suites_reset_the_admin_changes_from_the_shipped_default(self):
        issued = ops.issued_admin_password({"default_credentials": True}, "minted")
        new = ops.ui_admin_password({}, "minted")
        driver = _Driver(
            {issued: onboarding_run.CHANGE_PASSWORD_PATH, new: "/setup"}, after_change="/login"
        )

        assert onboarding_run.sign_in_as_admin(driver, "admin", issued, new) == new
        assert driver.page.filled["Current Password"] == ops.E2E_SEED_PASSWORD
        assert driver.page.logins == [ops.E2E_SEED_PASSWORD, new]


class TestTheAdminsNewPassword:
    def test_every_run_against_one_deploy_picks_the_same_one(self):
        assert ops.ui_admin_password({}, "minted") == ops.ui_admin_password({}, "minted")

    def test_a_redeploy_with_a_new_minted_password_moves_it(self):
        assert ops.ui_admin_password({}, "minted") != ops.ui_admin_password({}, "reminted")

    def test_it_clears_the_engines_floor_and_is_never_the_minted_one(self):
        password = ops.ui_admin_password({}, "minted")

        assert len(password) >= 12
        assert password != "minted"

    def test_a_password_the_caller_set_wins(self):
        settings = {ops.NEW_ADMIN_PASSWORD_VAR: "chosen-by-caller"}

        assert ops.ui_admin_password(settings, "minted") == "chosen-by-caller"

    def test_with_no_minted_password_it_still_clears_the_floor(self):
        assert len(ops.ui_admin_password({}, "")) >= 12


class _Response:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self._body


class TestWhichIssuedPasswordTheAdminIsOn:
    def test_the_suites_reset_leaves_it_on_the_shipped_default(self):
        assert ops.issued_admin_password({"default_credentials": True}, "minted") == ops.E2E_SEED_PASSWORD

    def test_otherwise_it_is_the_minted_one(self):
        assert ops.issued_admin_password({"default_credentials": False}, "minted") == "minted"

    def test_a_status_that_could_not_be_read_falls_back_to_the_minted_one(self):
        assert ops.issued_admin_password({}, "minted") == "minted"

    def test_the_status_is_read_off_the_engine(self):
        def opener(url, timeout):
            assert url == "http://engine/api/v1/auth/setup-status"
            return _Response(b'{"default_credentials": true}')

        assert ops.engine_setup_status("http://engine", opener=opener) == {
            "default_credentials": True
        }

    def test_an_engine_that_does_not_answer_gives_an_empty_status(self):
        def opener(url, timeout):
            raise ops.urllib.error.URLError("refused")

        assert ops.engine_setup_status("http://engine", opener=opener) == {}

    def test_a_body_that_is_not_a_document_gives_an_empty_status(self):
        assert ops.engine_setup_status("http://engine", opener=lambda *_a, **_k: _Response(b"[]")) == {}


class TestTheSourceTheConsoleCreates:
    def test_a_console_that_offers_the_table_choice_gets_the_shared_table(self):
        driver = _Driver({}, offered={onboarding_run.SHARED_TABLE})

        onboarding_run.fill_source_form(driver, "onboard1")

        assert driver.page.checked == [onboarding_run.SHARED_TABLE]
        assert driver.page.filled["Source Name"] == "onboard1"
        assert driver.page.filled["Value"] == "onboard1"

    def test_a_console_without_the_choice_is_filled_as_before(self):
        driver = _Driver({})

        onboarding_run.fill_source_form(driver, "onboard1")

        assert driver.page.checked == []
        assert driver.page.filled["Field"] == "app"


class TestTheFirstUsersPassword:
    def test_it_never_inherits_an_issued_shipped_default(self):
        password = onboarding_run.first_user_password(ops.E2E_SEED_PASSWORD, "the-admins-new-one")

        assert password == "the-admins-new-one"

    def test_without_a_new_admin_password_it_is_the_issued_one(self):
        assert onboarding_run.first_user_password("minted", "") == "minted"


class TestTheRunsVerdict:
    def test_any_failed_step_fails_the_run(self):
        results = [
            wizard.StepResult(wizard.WELCOME, "done", ""),
            wizard.StepResult(wizard.LOGIN, "failed", "no Skip for now control"),
        ]

        assert wizard.exit_code(results) == 1

    def test_a_tolerated_screen_does_not(self):
        results = [
            wizard.StepResult(wizard.WELCOME, "done", ""),
            wizard.StepResult(wizard.RESET_BREAK_GLASS, "tolerated", "this engine asks for it"),
        ]

        assert wizard.exit_code(results) == 0

    def test_the_table_carries_a_row_per_step(self):
        results = [
            wizard.StepResult(wizard.WELCOME, "done", "opened"),
            wizard.StepResult(wizard.COMPLETE, "done", "finished"),
        ]

        table = wizard.report_table(results)

        assert len(table.splitlines()) == 3
        assert wizard.WELCOME in table


class TestTheAccessSummaryTheDeployHandsOver:
    def test_it_carries_both_minted_logins_and_what_to_do_next(self):
        body = access_summary.render(
            "https://dfe.example", "https://dfe.example/api/v1",
            ("admin", "adminpw"), ("breakglass", "glasspw"),
        )

        assert "https://dfe.example" in body
        assert "adminpw" in body
        assert "glasspw" in body
        for line in access_summary.NEXT_STEPS:
            assert line in body

    def test_what_it_writes_is_what_it_reads_back(self):
        body = access_summary.render(
            "https://dfe.example", "https://dfe.example/api/v1",
            ("admin", "adminpw"), ("breakglass", "glasspw"),
        )

        assert access_summary.parse(body) == {
            access_summary.ADMIN_LABEL: ("admin", "adminpw"),
            access_summary.BREAKGLASS_LABEL: ("breakglass", "glasspw"),
        }

    def test_it_is_readable_only_by_the_person_who_ran_the_deploy(self, tmp_path):
        path = access_summary.write(tmp_path / "run" / access_summary.FILENAME, "body\n")

        assert path.stat().st_mode & 0o077 == 0

    def test_the_run_directory_is_the_repos_gitignored_scratch(self, tmp_path):
        assert access_summary.run_dir(tmp_path, "2.2.0", "slim") == tmp_path / ".tmp" / "2.2.0-slim"


class TestWhereTheAdminPasswordComesFrom:
    def _summary(self, tmp_path):
        return access_summary.write(
            tmp_path / access_summary.FILENAME,
            access_summary.render("u", "e", ("admin", "fromsummary"), ("breakglass", "bg")),
        )

    def test_the_launchers_summary_wins_so_a_test_signs_in_as_a_human_would(self, tmp_path):
        path = self._summary(tmp_path)

        assert ops.admin_credential(
            "docker",
            settings={ops.DOCKER_ADMIN_PASSWORD_KEY: "fromenv"},
            access_summary=path,
        ) == ("admin", "fromsummary")

    def test_a_summary_with_no_admin_row_refuses_rather_than_falling_through(self, tmp_path):
        path = tmp_path / access_summary.FILENAME
        path.write_text("# nothing here\n", encoding="utf-8")

        with pytest.raises(ops.CredentialNotFoundError, match="no admin row"):
            ops.admin_credential("docker", settings={}, access_summary=path)

    def test_docker_reads_the_key_make_init_wrote(self):
        assert ops.admin_credential(
            "docker",
            settings={
                ops.DOCKER_ADMIN_USER_KEY: "root",
                ops.DOCKER_ADMIN_PASSWORD_KEY: "minted",
            },
        ) == ("root", "minted")

    def test_a_fed_password_covers_a_lane_that_cannot_be_read_back(self):
        assert ops.admin_credential(
            "docker", settings={ops.FED_ADMIN_PASSWORD_VAR: "fedin"}
        ) == ("admin", "fedin")

    def test_a_docker_stack_with_nothing_to_read_says_where_to_look(self):
        with pytest.raises(ops.CredentialNotFoundError, match=ops.DOCKER_ADMIN_PASSWORD_KEY):
            ops.admin_credential("docker", settings={})

    def test_an_unknown_lane_is_refused(self):
        with pytest.raises(ValueError, match="docker or k8s"):
            ops.admin_credential("vm", settings={})


class TestTheBrowsersHostMap:
    """Chromium keeps only the last --host-resolver-rules, so the map is one flag."""

    def test_every_mapped_host_rides_in_one_flag(self):
        args = onboarding_run.resolver_args(
            [("dfe.example.test", "192.0.2.10"), ("hyperdx.example.test", "192.0.2.10")]
        )
        assert args == [
            "--host-resolver-rules=MAP dfe.example.test 192.0.2.10, "
            "MAP hyperdx.example.test 192.0.2.10"
        ]

    def test_no_map_adds_no_flag(self):
        assert onboarding_run.resolver_args([]) == []

    def test_every_route_host_under_the_consoles_domain_is_mapped(self, monkeypatch):
        """The console frames HyperDX on its own hostname, which must resolve too."""
        routes = {
            "items": [
                {"spec": {"hostnames": ["dfe.single.example.test"]}},
                {"spec": {"hostnames": ["hyperdx.single.example.test"]}},
                {"spec": {"hostnames": ["*.single.example.test"]}},
                {"spec": {"hostnames": ["other.example.org"]}},
            ]
        }
        monkeypatch.setattr(ops, "_kubectl_json", lambda _argv: (0, routes, ""))
        assert ops._route_hosts(["kubectl"], "dfe.single.example.test") == [
            "dfe.single.example.test",
            "hyperdx.single.example.test",
        ]

    def test_an_unreadable_route_list_still_maps_the_console(self, monkeypatch):
        monkeypatch.setattr(ops, "_kubectl_json", lambda _argv: (1, {}, "forbidden"))
        assert ops._route_hosts(["kubectl"], "dfe.single.example.test") == ["dfe.single.example.test"]


class TestSuiteOrder:
    def test_all_runs_onboarding_first(self):
        assert ops.suite_steps("all")[0] == ops.ONBOARDING_SUITE

    def test_all_proves_a_fresh_deploy_without_the_seeded_source(self):
        assert ops.suite_steps("all") == (ops.ONBOARDING_SUITE, "flows")

    def test_a_suite_that_needs_a_seeded_source_is_not_a_named_suite(self):
        assert "filebeat" not in ops.SUITES

    def test_a_named_suite_runs_only_itself(self):
        assert ops.suite_steps("flows") == ("flows",)
