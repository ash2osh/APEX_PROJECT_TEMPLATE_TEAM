import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".agents" / "skills" / "apex-background"
RENDERER = SKILL / "probe" / "render_findings.py"


class RenderFindingsTests(unittest.TestCase):
    def render(self, log: str, faults: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temporary:
            log_path = Path(temporary) / "log.csv"
            faults_path = Path(temporary) / "faults.csv"
            log_path.write_text(log, encoding="utf-8")
            faults_path.write_text(faults, encoding="utf-8")
            return subprocess.run(
                [
                    "python3",
                    str(RENDERER),
                    str(log_path),
                    str(faults_path),
                    "--apex",
                    "26.1.4",
                    "--database",
                    "FREEPDB1",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

    def test_matrix_has_one_column_per_context_in_first_seen_order(self) -> None:
        result = self.render(
            '"CONTEXT_NAME","PROBE_NAME","PROBE_VALUE","PROBE_ERROR"\n'
            '"SQLCL_APEX_SESSION","V(APP_SESSION)","1234",\n'
            '"SQLCL_APEX_SESSION","V(APP_SESSION)","1235",\n'
            '"WORKFLOW_START","V(APP_SESSION)","<null>",\n'
            '"SQLCL_APEX_SESSION","BIND :APEX$TASK_ID","",\n'
            '"WORKFLOW_START","DO_SUBSTITUTIONS(&APP_TITLE.)",,"ORA-06550: boom"\n',
            '"SOURCE","NAME","DETAIL"\n',
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertIn("APEX 26.1.4", result.stdout)
        self.assertIn("| Probe | SQLCL_APEX_SESSION | WORKFLOW_START |", lines)
        self.assertIn("| V(APP_SESSION) | 1234; 1235 | <null> |", lines)
        self.assertIn("| BIND :APEX$TASK_ID | <null> | - |", lines)
        self.assertIn("| DO_SUBSTITUTIONS(&APP_TITLE.) | - | error: ORA-06550: boom |", lines)
        self.assertIn("No faults recorded.", result.stdout)

    def test_pipes_in_values_are_escaped_and_faults_listed(self) -> None:
        result = self.render(
            '"CONTEXT_NAME","PROBE_NAME","PROBE_VALUE","PROBE_ERROR"\n'
            '"A","P","x|y",\n',
            '"SOURCE","NAME","DETAIL"\n"workflow activity","binds-start","faulted: ORA-01008"\n',
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("| P | x\\|y |", result.stdout)
        self.assertIn("- workflow activity `binds-start`: faulted: ORA-01008", result.stdout)

    def test_missing_header_is_rejected(self) -> None:
        result = self.render('"A","B"\n', '"SOURCE","NAME","DETAIL"\n')

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected CSV header", result.stderr)


class ProbeLayoutTests(unittest.TestCase):
    def test_probe_runner_is_executable(self) -> None:
        self.assertTrue(os.access(SKILL / "probe" / "run.sh", os.X_OK))

    def test_install_defines_every_probe_the_skill_relies_on(self) -> None:
        install = (SKILL / "probe" / "install.sql").read_text(encoding="utf-8")
        package_body = (SKILL / "probe" / "package-body.sql").read_text(encoding="utf-8")
        source = install + package_body
        for probe in (
            "V(APP_SESSION)",
            "V(APP_USER)",
            "V(PROBE_APP_ITEM)",
            "V(P1_PROBE_ITEM)",
            "APEX_APPLICATION.G_INSTANCE",
            "SYS_CONTEXT(APEX$SESSION,APP_SESSION)",
            "DO_SUBSTITUTIONS(&PROBE_SUBST.)",
            "USERENV BG_JOB_ID",
            "USERENV MODULE",
        ):
            with self.subTest(probe=probe):
                self.assertIn(f"'{probe}'", source)
        self.assertIn("PRAGMA AUTONOMOUS_TRANSACTION", package_body)
        self.assertIn("SET DEFINE OFF", install)
        self.assertIn("apex_bg_probe_one_active_uq", install)
        self.assertIn("WHERE run_state = 'RUNNING'", source)
        self.assertIn("@@package-body.sql", install)
        self.assertIn("PROCEDURE complete_run", package_body)

    def test_skill_audits_all_official_builtin_substitution_names(self) -> None:
        skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        official_names = (
            "APEX_CSP_DISPLAY_NONE", "APEX_FILES", "APEX$ROW_NUM", "APEX$ROW_SELECTOR", "APEX$ROW_STATUS",
            "APP_ID", "APP_ALIAS", *(f"APP_AJAX_X{i:02d}" for i in range(1, 11)),
            "APP_BUILDER_SESSION", "APP_DATE_TIME_FORMAT", "APP_FILES", "APP_NLS_DATE_FORMAT",
            "APP_NLS_TIMESTAMP_FORMAT", "APP_NLS_TIMESTAMP_TZ_FORMAT", "APP_PAGE_ALIAS", "APP_PAGE_ID",
            "APP_REGION_DOM_ID", "APP_REGION_ID", "APP_REGION_STATIC_ID", "APP_REQUEST_DATA_HASH",
            "APP_SESSION", "SESSION", "APP_SESSION_VISIBLE", "APP_TEXT$Message_Name", "APP_TEXT$Message_Name$Lang",
            "APP_TITLE", "APP_UNIQUE_PAGE_ID", "APP_USER", "APP_VERSION", "AUTHENTICATED_URL_PREFIX",
            "BROWSER_LANGUAGE", "CURRENT_PARENT_TAB_TEXT", "DEBUG", "DEFAULT_THEME_FILES", "HOME_LINK",
            "JET_BASE_DIRECTORY", "JET_CSS_DIRECTORY", "JET_JS_DIRECTORY", "LOGIN_URL", "LOGOUT_URL",
            "MAIN_APP_ID", "OWNER", "PRINTER_FRIENDLY", "PROXY_SERVER", "PUBLIC_URL_PREFIX", "REQUEST",
            "SCHEMA OWNER", "SQLERRM", "SYSDATE_YYYYMMDD", "THEME_DB_FILES", "THEME_FILES",
            "WORKSPACE_FILES", "WORKSPACE_ID",
        )
        self.assertEqual(len(official_names), 62)
        missing = [name for name in official_names if name not in skill]
        self.assertFalse(missing, f"official built-ins missing from shipped skill: {missing}")

    def test_probe_captures_official_app_substitutions_and_app_text(self) -> None:
        install = (SKILL / "probe" / "install.sql").read_text(encoding="utf-8")
        package_body = (SKILL / "probe" / "package-body.sql").read_text(encoding="utf-8")
        source = install + package_body
        message = SKILL / "probe" / "app" / "shared-components" / "messages.apx"
        probed_names = (
            "APEX_FILES", "APEX$ROW_NUM", "APEX$ROW_SELECTOR", "APEX$ROW_STATUS", "APP_ID", "APP_ALIAS",
            *(f"APP_AJAX_X{i:02d}" for i in range(1, 11)), "APP_BUILDER_SESSION", "APP_DATE_TIME_FORMAT",
            "APP_FILES", "APP_NLS_DATE_FORMAT", "APP_NLS_TIMESTAMP_FORMAT", "APP_NLS_TIMESTAMP_TZ_FORMAT",
            "APP_PAGE_ALIAS", "APP_PAGE_ID", "APP_REGION_DOM_ID", "APP_REGION_ID", "APP_REGION_STATIC_ID",
            "APP_REQUEST_DATA_HASH", "APP_SESSION", "SESSION", "APP_SESSION_VISIBLE", "APP_TITLE",
            "APP_UNIQUE_PAGE_ID", "APP_USER", "AUTHENTICATED_URL_PREFIX", "BROWSER_LANGUAGE",
            "CURRENT_PARENT_TAB_TEXT", "DEBUG", "HOME_LINK", "LOGIN_URL", "LOGOUT_URL", "MAIN_APP_ID",
            "PUBLIC_URL_PREFIX", "REQUEST", "WORKSPACE_FILES", "WORKSPACE_ID", "APP_TEXT$PROBE_MESSAGE",
            "APP_TEXT$PROBE_MESSAGE$EN", "APP_IMAGES", "IMAGE_PREFIX", "THEME_DB_IMAGES", "THEME_IMAGES",
            "WORKSPACE_IMAGE",
        )
        missing = [name for name in probed_names if f"'{name}'" not in source]
        self.assertFalse(missing, f"official ampersand built-ins missing from probe: {missing}")
        for probe in (
            "APEX_APPLICATION.G_FLOW_SCHEMA_OWNER", "APEX_APPLICATION.G_PROXY_SERVER",
            "APEX_APPLICATION.G_SYSDATE", "V(SYSDATE_YYYYMMDD)",
        ):
            with self.subTest(probe=probe):
                self.assertIn(f"'{probe}'", source)
        self.assertIn("APP_TITLE", source)
        self.assertTrue(message.exists())
        self.assertIn("textMessage PROBE_MESSAGE", message.read_text(encoding="utf-8"))

    def test_probe_covers_all_official_workflow_and_task_substitutions(self) -> None:
        app = SKILL / "probe" / "app"
        source = "\n".join(p.read_text(encoding="utf-8") for p in app.rglob("*.apx"))
        names = (
            "APEX$WORKFLOW_ACTIVITY_ID", "APEX$WORKFLOW_CREATED_ON", "APEX$WORKFLOW_DETAIL_PK",
            "APEX$WORKFLOW_ID", "APEX$WORKFLOW_INITIATOR", "APEX$WORKFLOW_STATE",
            "APEX$TASK_CREATED_ON", "APEX$TASK_DUE_ON", "APEX$TASK_ID", "APEX$TASK_INITIATOR",
            "APEX$TASK_MAX_RENEWAL_COUNT", "APEX$TASK_OUTCOME", "APEX$TASK_OWNER", "APEX$TASK_PK",
            "APEX$TASK_PREVIOUS_ID", "APEX$TASK_RENEWAL_COUNT", "APEX$TASK_STATE", "APEX$TASK_SUBJECT",
            "APEX$TASK_TEXT",
        )
        missing = [name for name in names if name not in source]
        self.assertFalse(missing, f"official workflow/task names missing from probe: {missing}")
        skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("`APEX$TASK_TEXT`", skill)

    def test_uninstall_removes_every_installed_object(self) -> None:
        uninstall = (SKILL / "probe" / "uninstall.sql").read_text(encoding="utf-8")
        for statement in (
            "DROP PACKAGE apex_bg_probe",
            "DROP TABLE apex_bg_probe_log",
            "DROP TABLE apex_bg_probe_run",
        ):
            with self.subTest(statement=statement):
                self.assertIn(statement, uninstall)

    def test_uninstall_checks_object_ownership_before_dropping(self) -> None:
        uninstall = (SKILL / "probe" / "uninstall.sql").read_text(encoding="utf-8")
        self.assertIn("APEX_BG_PROBE_OWNER_V1", uninstall)
        self.assertIn("user_tab_comments", uninstall)
        self.assertIn("user_source", uninstall)
        self.assertIn("Missing objects are accepted", uninstall)

    def test_probe_app_defines_every_background_context(self) -> None:
        app = SKILL / "probe" / "app"
        source = "\n".join(p.read_text(encoding="utf-8") for p in app.rglob("*.apx"))
        for context in (
            "AUTOMATION_ON_DEMAND'", "AUTOMATION_ON_DEMAND_BACKGROUND'", "AUTOMATION_SCHEDULED'",
            "TASK_ACTION_CREATE'", "TASK_ACTION_COMPLETE'", "WORKFLOW_START'", "WORKFLOW_AFTER_WAIT'",
            "TASK_ACTION_COMMENT'", "WORKFLOW_AFTER_TASK'", "PAGE_PROCESS_FOREGROUND'", "EXECUTION_CHAIN_BACKGROUND'",
        ):
            with self.subTest(context=context):
                self.assertIn("'" + context, source)
                self.assertIn(f"record_context_complete('{context[:-1]}", source)
        application = (app / "application.apx").read_text(encoding="utf-8")
        self.assertTrue(application.startswith("app APEX-BG-PROBE ("))
        self.assertIn("substitution PROBE_SUBST (", application)
        self.assertIn("scheme: @no-authentication", application)
        self.assertIn("loginUrl:", application)
        page = (app / "pages" / "p00001-home.apx").read_text(encoding="utf-8")
        self.assertIn("type: breadcrumb", page)
        self.assertIn("breadcrumb: @breadcrumb", page)
        self.assertIn("helpText:", page)
        self.assertIn("scheduleStatus: disabled", source)

    def test_probe_runner_requires_a_confirmed_development_target(self) -> None:
        runner = (SKILL / "probe" / "run.sh").read_text(encoding="utf-8")
        preflight = (SKILL / "probe" / "target-check.sql").read_text(encoding="utf-8")
        self.assertIn("DB_ENVIRONMENT=development", runner)
        self.assertIn("APEX_SQLCL_CONNECTION", runner)
        self.assertIn("APEX_PARSING_SCHEMA", runner)
        self.assertIn("APEX_EXPECTED_USER", runner)
        self.assertIn('[[ -t 0 ]] || fail', runner)
        self.assertIn("Type exactly:", runner)
        self.assertIn("APEX_BG_PROBE_PREFLIGHT_OK", preflight)
        self.assertIn("Application ID 9901 is already in use", preflight)
        self.assertIn("APEX-BG-PROBE", preflight)
        self.assertIn("APEX_BG_PROBE_OBJECTS:", preflight)
        self.assertIn("l_invalid_object_count > 0", preflight)
        self.assertIn("EXECUTE IMMEDIATE", preflight)
        self.assertIn("FROM apex_bg_probe_run", preflight)
        self.assertIn("Missing objects are accepted", (SKILL / "probe" / "uninstall.sql").read_text(encoding="utf-8"))

    def test_probe_runner_preserves_local_state_and_blocks_unsafe_paths(self) -> None:
        runner = (SKILL / "probe" / "run.sh").read_text(encoding="utf-8")
        self.assertNotIn("rm -rf", runner)
        self.assertIn("-L \"$state_dir\"", runner)
        self.assertIn("safe_output", runner)
        self.assertIn("refusing symlink", runner)
        self.assertIn("Local .run files were retained", runner)
        self.assertIn("Resuming import; the marked probe objects are already installed.", runner)
        self.assertIn("app.previous-", runner)

    def test_workflow_and_finish_scope_side_effects_to_one_run(self) -> None:
        contexts = (SKILL / "probe" / "contexts.sql").read_text(encoding="utf-8")
        finish = (SKILL / "probe" / "finish.sql").read_text(encoding="utf-8")
        self.assertIn("p_parameters => apex_workflow.t_workflow_parameters", contexts)
        self.assertIn("varchar2_value => 'from-sqlcl-workflow'", contexts)
        self.assertIn("p_detail_pk => TO_CHAR(l_run_id)", contexts)
        self.assertIn("active_slot", (SKILL / "probe" / "install.sql").read_text(encoding="utf-8"))
        self.assertIn("detail_pk = TO_CHAR(l_run_id)", finish)
        self.assertIn("FINISH_ATTEMPT_START", finish)
        self.assertIn("log_id > (", finish)
        self.assertGreaterEqual(finish.count("context_name = 'FINISH_ATTEMPT_START'"), 4)
        self.assertIn("SET VERIFY OFF", finish)
        probe_faults = finish.split("SELECT 'probe' AS source", 1)[1].split("UNION ALL", 1)[0]
        self.assertIn("l.context_name <> 'FINISH'", probe_faults)
        self.assertIn("t.workflow_id = w.workflow_id", finish)
        self.assertIn("w.workflow_def_static_id = 'bg-probe-workflow'", finish)
        schedule_disable = finish.split("PROCEDURE disable_schedule IS", 1)[1].split(
            "END disable_schedule;", 1
        )[0]
        self.assertIn("apex_session.create_session", schedule_disable)
        self.assertLess(
            schedule_disable.index("apex_session.create_session"),
            schedule_disable.index("apex_automation.disable"),
        )
        self.assertIn("apex_automation.disable", finish)
        self.assertIn("apex_bg_probe.complete_run", finish)
        self.assertIn("apex_human_task.add_task_comment", contexts)
        self.assertIn("apex_human_task.add_task_comment", finish)

    def test_workflow_parameter_uses_the_documented_typed_value(self) -> None:
        contexts = (SKILL / "probe" / "contexts.sql").read_text(encoding="utf-8")
        self.assertIn("value => apex_session_state.t_value(", contexts)
        self.assertIn("data_type => apex_session_state.c_data_type_varchar2", contexts)
        self.assertIn("varchar2_value => 'from-sqlcl-workflow'", contexts)

    def test_complete_run_captures_active_id_before_updating_run_table(self) -> None:
        package_body = (SKILL / "probe" / "package-body.sql").read_text(encoding="utf-8")
        complete_run = package_body.split("PROCEDURE complete_run IS", 1)[1].split(
            "END complete_run;", 1
        )[0]
        self.assertIn("SELECT run_id INTO l_run_id", complete_run)
        self.assertIn("WHERE run_id = l_run_id", complete_run)
        self.assertNotIn("WHERE run_id = current_run", complete_run)

    def test_finish_waits_for_completion_markers_and_exports_partial_evidence(self) -> None:
        finish = (SKILL / "probe" / "finish.sql").read_text(encoding="utf-8")
        package_body = (SKILL / "probe" / "package-body.sql").read_text(encoding="utf-8")
        runner = (SKILL / "probe" / "run.sh").read_text(encoding="utf-8")

        self.assertIn("probe_name = 'CONTEXT_COMPLETE'", finish)
        self.assertIn("record_context_complete", package_body)
        self.assertIn("record_issue", package_body)
        self.assertIn("get_current_execution", package_body.lower())
        self.assertIn("get_execution", finish.lower())
        self.assertLess(finish.index("SPOOL faults.csv"), finish.index("finish incomplete; evidence was exported"))
        self.assertIn("evidence was exported", finish)
        self.assertIn("APEX_BG_PROBE_PACKAGE_BODY_READY", runner)
        self.assertIn("finish_status=0", runner)
        self.assertIn("report the retained evidence", runner)

    def test_background_chain_failure_status_is_captured_before_bind_stage(self) -> None:
        page = (SKILL / "probe" / "app" / "pages" / "p00001-home.apx").read_text(encoding="utf-8")
        finish = (SKILL / "probe" / "finish.sql").read_text(encoding="utf-8").lower()

        self.assertLess(page.index("record_background_execution"), page.index("process binds-background"))
        self.assertIn("c_status_failed", finish)
        self.assertIn("last_status_message", finish)

    def test_finish_uses_verified_fault_view_columns_and_run_scope(self) -> None:
        finish = (SKILL / "probe" / "finish.sql").read_text(encoding="utf-8")
        install = (SKILL / "probe" / "install.sql").read_text(encoding="utf-8")

        self.assertIn("a.static_id", finish)
        self.assertIn("a.state IN ('FAULTED', 'ERROR')", finish)
        self.assertIn("a.error_message", finish)
        self.assertIn("FROM apex_automation_msg_log l", finish)
        self.assertIn("JOIN apex_appl_automations m ON m.automation_id = l.automation_id", finish)
        self.assertIn("m.application_id = &&app_id", finish)
        self.assertIn("l.message_type = 'ERROR'", finish)
        self.assertIn("l.message_timestamp >= r.started_at", finish)
        self.assertIn("started_at TIMESTAMP WITH TIME ZONE", install)


class SkillContractTests(unittest.TestCase):
    def test_skill_frontmatter_names_the_directory(self) -> None:
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\nname: apex-background\ndescription: "))
        self.assertIn("\n---\n", text[4:])

    def test_claude_copy_is_byte_identical(self) -> None:
        self.assertEqual(
            (ROOT / ".claude" / "skills" / "apex-background" / "SKILL.md").read_bytes(),
            (SKILL / "SKILL.md").read_bytes(),
        )

    def test_skill_cites_findings_for_every_context(self) -> None:
        skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        findings = (SKILL / "findings-apex-26.1.md").read_text(encoding="utf-8")
        header = next(line for line in findings.splitlines() if line.startswith("| Probe |"))
        contexts = [context.strip() for context in header.strip("|").split("|")[1:]]
        self.assertGreaterEqual(len(contexts), 13)
        for context in contexts:
            with self.subTest(context=context):
                self.assertIn(context, skill)
        self.assertIn("findings-apex-26.1.md", skill)
        self.assertIn("APEX 26.1.4", skill)

    def test_skill_marks_unobserved_task_comment_session_and_unverified_actions(self) -> None:
        skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("TASK_ACTION_COMMENT", skill)
        self.assertIn("not verified", skill.lower())
        self.assertIn("APEX$TASK_TEXT", skill)
        self.assertIn("Request Information", skill)
        self.assertIn("Submit Information", skill)
        for name in (
            "APEX$WORKFLOW_ACTIVITY_ID", "APEX$WORKFLOW_CREATED_ON", "APEX$WORKFLOW_DETAIL_PK",
            "APEX$WORKFLOW_ID", "APEX$WORKFLOW_INITIATOR", "APEX$WORKFLOW_STATE",
            "APEX$TASK_CREATED_ON", "APEX$TASK_DUE_ON", "APEX$TASK_ID", "APEX$TASK_INITIATOR",
            "APEX$TASK_MAX_RENEWAL_COUNT", "APEX$TASK_OUTCOME", "APEX$TASK_OWNER", "APEX$TASK_PK",
            "APEX$TASK_PREVIOUS_ID", "APEX$TASK_RENEWAL_COUNT", "APEX$TASK_STATE", "APEX$TASK_SUBJECT",
        ):
            with self.subTest(name=name):
                self.assertIn(name, skill)

    def test_skill_session_summary_matches_findings(self) -> None:
        skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        findings = (SKILL / "findings-apex-26.1.md").read_text(encoding="utf-8")
        header = next(line for line in findings.splitlines() if line.startswith("| Probe |"))
        contexts = [context.strip() for context in header.strip("|").split("|")[1:]]
        values = next(line for line in findings.splitlines() if line.startswith("| V(APP_SESSION) |"))
        cells = [value.strip() for value in values.strip("|").split("|")[1:]]
        observed = dict(zip(contexts, cells, strict=True))
        self.assertIn("<null>", observed["SQLCL_NO_SESSION"])
        self.assertNotIn("<null>", observed["AUTOMATION_ON_DEMAND_BACKGROUND"])
        self.assertNotIn("<null>", observed["WORKFLOW_AFTER_TASK"])
        self.assertIn("nobody", skill)
        self.assertIn("TASK_ACTION_COMMENT", skill)
        self.assertIn("Generic session values", skill)

    def test_agents_points_at_the_skill(self) -> None:
        self.assertIn(".agents/skills/apex-background/SKILL.md", (ROOT / "AGENTS.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
