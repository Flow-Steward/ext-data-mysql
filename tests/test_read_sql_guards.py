"""Read validation must reject dialect-ambiguous escapes and unbounded LIMITs."""

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def guarded_runtime():
    root = Path(__file__).resolve().parents[1]
    path = root / ("sql_runtime.py" if (root / "sql_runtime.py").exists() else "main.py")
    spec = importlib.util.spec_from_file_location("read_guard_runtime", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "sql",
    [
        r"SELECT '\'; DELETE FROM products; -- '",
        r"SELECT '\', pg_sleep(30) -- '",
        r"SELECT '\' LIMIT 1",
        r"SELECT 1 -- \\ ignored",
    ],
)
def test_backslash_is_rejected_before_every_read_lexer_or_rewrite(guarded_runtime, sql):
    module = guarded_runtime
    prebuilt = sorted(module.TOOLBOX_DATASET_PREBUILTS)[0]
    calls = [
        module._sql_tokens,
        module._strip_sql_comments,
        module._placeholder_occurrences,
        lambda value: module._template_sql({"sql": value}),
        lambda value: module._limited_sql(prebuilt, value, 100),
        lambda value: module._compile_bound_query(prebuilt, value, {}),
    ]
    for call in calls:
        with pytest.raises(ValueError, match="backslash"):
            call(sql)


@pytest.mark.parametrize(
    "clause",
    [
        "LIMIT 1+1000000",
        "LIMIT (1+1000000)",
        "LIMIT 1,1000000",
        "LIMIT 1 OFFSET 0+1000000",
        "LIMIT 1 OFFSET (2)",
        "LIMIT 1 UNION SELECT sku FROM products",
    ],
)
def test_limit_is_a_complete_literal_clause(guarded_runtime, clause):
    module = guarded_runtime
    for prebuilt in module.TOOLBOX_DATASET_PREBUILTS - {"mssql", "cloud-sql-mssql"}:
        with pytest.raises(ValueError):
            module._limited_sql(prebuilt, "SELECT sku FROM products " + clause, 100)


def test_literal_offset_and_nested_limits_keep_outer_cap(guarded_runtime):
    module = guarded_runtime
    for prebuilt in module.TOOLBOX_DATASET_PREBUILTS - {"mssql", "cloud-sql-mssql"}:
        assert module._limited_sql(
            prebuilt, "SELECT sku FROM products LIMIT 1 OFFSET 2", 100
        ).endswith("LIMIT 1 OFFSET 2")
        assert module._limited_sql(
            prebuilt, "SELECT * FROM (SELECT sku FROM products LIMIT 1+1000000) AS nested", 100
        ).endswith("AS nested LIMIT 100")
    for prebuilt in module.TOOLBOX_DATASET_PREBUILTS & {"mssql", "cloud-sql-mssql"}:
        with pytest.raises(ValueError, match="set queries"):
            module._limited_sql(
                prebuilt, "SELECT sku FROM products UNION SELECT sku FROM products", 100
            )


@pytest.mark.parametrize("test_mode", [False, True])
def test_unsafe_read_never_reaches_provider_even_without_effect_claim(
    guarded_runtime, monkeypatch, test_mode
):
    module = guarded_runtime
    prebuilt = sorted(module.TOOLBOX_DATASET_PREBUILTS)[0]
    monkeypatch.setattr(module, "_connection_config", lambda request: {"prebuilt": prebuilt})
    monkeypatch.setattr(
        module,
        "_query_definition",
        lambda request: {"sql": r"SELECT '\'; DELETE FROM products; -- '"},
    )
    monkeypatch.setattr(
        module, "_invoke_toolbox", lambda *a, **k: pytest.fail("unsafe read reached a driver")
    )
    with pytest.raises(ValueError, match="backslash"):
        module._execute_sql(
            {"runtime_context": {"test_mode": test_mode}, "action": {"input": {}}}, preview=False
        )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT x$$ FROM products; DELETE FROM products; SELECT x$$ FROM products WHERE sku='a'",
        "SELECT x$tag$ FROM products; DELETE FROM products; SELECT x$tag$ FROM products WHERE sku='a'",
    ],
)
def test_dollar_quote_confusion_is_rejected_before_all_read_entrypoints(guarded_runtime, sql):
    module = guarded_runtime
    prebuilt = sorted(module.TOOLBOX_DATASET_PREBUILTS)[0]
    calls = [
        module._sql_tokens,
        module._strip_sql_comments,
        module._placeholder_occurrences,
        lambda value: module._template_sql({"sql": value}),
        lambda value: module._limited_sql(prebuilt, value, 100),
        lambda value: module._compile_bound_query(prebuilt, value, {}),
    ]
    for call in calls:
        with pytest.raises(ValueError, match="dollar-quote"):
            call(sql)


@pytest.mark.parametrize("test_mode", [False, True])
def test_dollar_script_never_invokes_provider_without_write_claim(
    guarded_runtime, monkeypatch, test_mode
):
    module = guarded_runtime
    prebuilt = sorted(module.TOOLBOX_DATASET_PREBUILTS)[0]
    monkeypatch.setattr(module, "_connection_config", lambda request: {"prebuilt": prebuilt})
    monkeypatch.setattr(
        module,
        "_query_definition",
        lambda request: {
            "sql": "SELECT x$$ FROM products; DELETE FROM products; SELECT x$$ FROM products WHERE sku='a'"
        },
    )
    monkeypatch.setattr(
        module,
        "_invoke_toolbox",
        lambda *a, **k: pytest.fail("read script reached provider without a write claim"),
    )
    with pytest.raises(ValueError, match="dollar-quote"):
        module._execute_sql(
            {"runtime_context": {"test_mode": test_mode}, "action": {"input": {}}}, preview=False
        )


def test_bound_values_may_contain_backslashes_and_dollar_text(guarded_runtime):
    module = guarded_runtime
    import json

    for prebuilt in module.TOOLBOX_DATASET_PREBUILTS:
        value = r"C:\folder $$ $tag$"
        bound = module._compile_bound_query(prebuilt, "SELECT :value AS value", {"value": value})
        assert json.loads(bound["params_json"]) == {"value": value}
        assert value not in bound["sql"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT $тэг$'$тэг$; DELETE FROM products; -- ' FROM products WHERE sku='a'",
        "SELECT $💣$'$💣$; DELETE FROM products; -- ' FROM products WHERE sku='a'",
        "SELECT '$' AS value LIMIT 1",
        "SELECT price$usd FROM products LIMIT 1",
    ],
)
def test_every_raw_dollar_is_rejected_before_read_parsing(guarded_runtime, sql):
    module = guarded_runtime
    prebuilt = sorted(module.TOOLBOX_DATASET_PREBUILTS)[0]
    for call in [
        module._sql_tokens,
        module._strip_sql_comments,
        module._placeholder_occurrences,
        lambda value: module._template_sql({"sql": value}),
        lambda value: module._limited_sql(prebuilt, value, 100),
        lambda value: module._compile_bound_query(prebuilt, value, {}),
    ]:
        with pytest.raises(ValueError, match="raw dollar"):
            call(sql)


def test_generated_numbered_placeholders_and_unicode_dollar_values_remain_valid(guarded_runtime):
    module = guarded_runtime
    import json

    value = "$тэг$ $💣$ $"
    for prebuilt in module.TOOLBOX_DATASET_PREBUILTS:
        bound = module._compile_bound_query(prebuilt, "SELECT :value AS value", {"value": value})
        assert json.loads(bound["params_json"]) == {"value": value}
        assert value not in bound["sql"]


def test_dataset_grants_preserve_spelling_and_do_not_make_short_names(guarded_runtime):
    request = {
        "action": {
            "input": {
                "dataset": {
                    "resource_scope_jsonb": {
                        "resources": [
                            {"resource_id": "Audit.Events", "columns": ["EventID"]},
                            {"resource_id": "Events", "columns": ["summary"]},
                            {"resource_id": "events", "columns": ["kind"]},
                        ],
                    },
                },
            },
        },
    }

    assert guarded_runtime._dataset_scope(request) == {
        "Audit.Events": {"EventID"},
        "Events": {"summary"},
        "events": {"kind"},
    }


def test_scope_tracks_qualified_columns_and_ignores_output_labels(guarded_runtime):
    guarded_runtime._validate_query_scope(
        "SELECT e.event_id AS label, upper(e.summary) AS display_summary "
        "FROM Events AS e WHERE e.event_id = :event_id ORDER BY e.event_id LIMIT 2",
        {"Events": {"event_id", "summary"}},
    )


def test_scope_binds_qualified_table_to_the_exact_active_database(guarded_runtime):
    guarded_runtime._validate_query_scope(
        "SELECT Events.event_id FROM Reporting.Events LIMIT 2",
        {"Events": {"event_id"}},
        database="Reporting",
    )


def test_scope_accepts_quoted_identifiers_and_keeps_periods_inside_names(guarded_runtime):
    guarded_runtime._validate_query_scope(
        "SELECT e.`order` FROM `Events.Archive` e LIMIT 2",
        {"Events.Archive": {"order"}},
    )


def test_scope_preserves_ordinary_join_and_aggregate_queries(guarded_runtime):
    guarded_runtime._validate_query_scope(
        "SELECT o.customer_id, COUNT(*) AS order_count FROM Orders o "
        "JOIN Customers c ON o.customer_id = c.customer_id "
        "WHERE o.active = true GROUP BY o.customer_id ORDER BY o.customer_id LIMIT 10",
        {"Orders": {"customer_id", "active"}, "Customers": {"customer_id"}},
    )


def test_dataset_introspection_uses_selected_database(guarded_runtime, monkeypatch):
    prebuilt = "mysql" if "mysql" in guarded_runtime.TOOLBOX_DATASET_PREBUILTS else "oceanbase"
    monkeypatch.setattr(
        guarded_runtime,
        "_invoke_toolbox",
        lambda *args, **kwargs: [
            {"schema_name": "Reporting", "object_name": "Events"},
            {"schema_name": "Archive", "object_name": "Events"},
        ],
    )
    request = {
        "action": {"input": {"connection_config": {"prebuilt": prebuilt, "database": "Reporting"}}},
    }

    snapshot = guarded_runtime._introspect(request)

    assert [
        (row["resource_id"], row["metadata"]["toolbox_schema_name"])
        for row in snapshot["resources"]
    ] == [("Events", "Reporting")]


def test_dataset_read_preserves_bound_sql_for_the_saved_resource(guarded_runtime, monkeypatch):
    prebuilt = "mysql" if "mysql" in guarded_runtime.TOOLBOX_DATASET_PREBUILTS else "oceanbase"
    calls = []

    def provider(_config, _secrets, _operation, query, **_kwargs):
        calls.append(query)
        return [{"event_id": 5}]

    monkeypatch.setattr(guarded_runtime, "_invoke_toolbox", provider)
    request = {
        "action": {
            "input": {
                "connection_config": {"prebuilt": prebuilt, "database": "Reporting"},
                "dataset": {
                    "resource_scope_jsonb": {
                        "resources": [{"resource_id": "Events", "columns": ["event_id"]}],
                    },
                    "schema_snapshot_jsonb": {
                        "resources": [
                            {
                                "resource_id": "Events",
                                "metadata": {"toolbox_schema_name": "Reporting"},
                            }
                        ],
                    },
                },
                "query_template": {
                    "query_definition_jsonb": {
                        "sql": "SELECT CAST(event_id AS SIGNED) AS display_id FROM Events "
                        "WHERE event_id = :id ORDER BY display_id"
                    },
                    "param_schema_jsonb": {"properties": {"id": {"type": "integer"}}},
                },
                "params": {"id": 5},
            },
        },
    }

    assert guarded_runtime._execute_sql(request, preview=True)["rows"] == [{"event_id": 5}]
    assert calls[0]["sql"] == (
        "SELECT CAST(event_id AS SIGNED) AS display_id FROM Events "
        "WHERE event_id = ? ORDER BY 1 LIMIT 10"
    )
    assert calls[0]["param_order"] == ["id"]


def test_legacy_scope_keeps_duplicate_table_identity_ambiguous(guarded_runtime):
    dataset = {
        "schema_snapshot_jsonb": {
            "resources": [
                {"resource_id": "Events", "metadata": {"toolbox_schema_name": "Reporting"}},
                {"resource_id": "Events", "metadata": {"toolbox_schema_name": "Archive"}},
                {"resource_id": "Customers", "metadata": {"toolbox_schema_name": "Reporting"}},
            ],
        },
    }
    request = {"action": {"input": {"dataset": dataset}}}

    assert guarded_runtime._dataset_resource_databases(request) == {
        "Events": None,
        "Customers": "Reporting",
    }


def test_dataset_functions_are_native_to_both_supported_dialects(guarded_runtime):
    functions = guarded_runtime._READ_ONLY_FUNCTIONS

    assert {"count", "lower", "upper", "cast", "extract"} <= functions
    assert "date_trunc" not in functions
    assert "row_number" not in functions


def test_schema_scope_reserves_stateful_sequence_pseudocolumns(guarded_runtime):
    from read_sql import is_sequence_pseudocolumn

    assert is_sequence_pseudocolumn(("sequence_name", "NEXTVAL"))
    assert is_sequence_pseudocolumn(("reporting", "sequence_name", "CURRVAL"))
    assert not is_sequence_pseudocolumn(("sequence_name", "event_id"))
    assert not is_sequence_pseudocolumn(("nextval",))
