#!/usr/bin/env python3
# Status: production
# Path: imported by — cli.py
"""CLI experiment management — list, compare, active, adopt commands."""
import json

from lib.db import psql as _sql, psql_json as _psql_json, esc_sql


def cmd_experiment_list(args):
    """List experiments from experiment_registry."""
    limit = "" if args.all else f"LIMIT {args.limit or 20}"
    where = ""
    if args.category:
        cat = esc_sql(args.category)
        where = f"WHERE category = '{cat}'"
    sql = f"""SELECT id, created_at, experiment_id, category, subcategory, verdict,
       substring(rationale,1,60) AS rationale,
       results->>'decode_tps' AS tps
    FROM experiment_registry
    {where}
    ORDER BY created_at DESC
    {limit}"""
    rows = _psql_json(sql)
    if not rows:
        print("(empty)")
        return
    print(f"{'ID':<5} {'Created':<20} {'Experiment ID':<40} {'Verdict':<12} {'TPS':<8} {'Rationale'}")
    print("-" * 130)
    for r in rows:
        print(f"{r['id']:<5} {str(r['created_at'])[:19]:<20} {str(r['experiment_id'])[:38]:<40} "
              f"{str(r['verdict']):<12} {str(r['tps'] or '?'):<8} {str(r['rationale'] or '')[:50]}")


def cmd_experiment_compare(args):
    """Compare multiple experiments side by side."""
    ids = args.experiment_ids
    if not ids:
        print("ERROR: at least one experiment_id required")
        return
    placeholders = ", ".join(f"'{esc_sql(eid)}'" for eid in ids)
    sql = f"""SELECT experiment_id, created_at, category, subcategory, verdict, rationale, config, results
    FROM experiment_registry
    WHERE experiment_id IN ({placeholders})
    ORDER BY created_at DESC"""
    rows = _psql_json(sql)
    if not rows:
        print("(empty)")
        return
    for r in rows:
        eid = r["experiment_id"]
        print(f"=== {eid} ===")
        print(f"  Created:   {r['created_at']}")
        print(f"  Category:  {r['category']} / {r['subcategory']}")
        print(f"  Verdict:   {r['verdict']}")
        print(f"  Rationale: {r.get('rationale', '')}")
        print(f"  Config:")
        cfg = r.get("config", {})
        if isinstance(cfg, dict):
            for k, v in cfg.items():
                print(f"    {k}: {v}")
        print(f"  Results:")
        res = r.get("results", {})
        if isinstance(res, dict):
            for k, v in res.items():
                if k != "results":
                    print(f"    {k}: {v}")
        print()


def cmd_experiment_active(args):
    """Show active_config entries."""
    sql = """SELECT component, config, applied_at, experiment_id, rationale FROM active_config ORDER BY component"""
    rows = _psql_json(sql)
    if not rows:
        print("(empty)")
        return
    for r in rows:
        comp = r["component"]
        print(f"=== {comp} ===")
        print(f"  Applied:     {r['applied_at']}")
        print(f"  Experiment:  {r.get('experiment_id', '-')}")
        print(f"  Rationale:   {r.get('rationale', '')}")
        print(f"  Config:")
        cfg = r.get("config", {})
        if isinstance(cfg, dict):
            for k, v in cfg.items():
                print(f"    {k}: {v}")
        print()


def cmd_experiment_adopt(args):
    """Adopt an experiment result as active_config."""
    eid = args.experiment_id
    component = args.component
    rows = _psql_json(
        f"SELECT experiment_id, config, rationale FROM experiment_registry WHERE experiment_id = '{esc_sql(eid)}'"
    )
    if not rows:
        print(f"ERROR: experiment '{eid}' not found")
        return
    row = rows[0]
    config_json = json.dumps(row["config"])
    rationale = esc_sql(row.get("rationale") or "")

    sql = f"""INSERT INTO active_config (component, config, experiment_id, rationale)
    VALUES ('{esc_sql(component)}', $json${config_json}$json$::jsonb, '{esc_sql(eid)}', '{rationale}')
    ON CONFLICT (component) DO UPDATE SET
        config = EXCLUDED.config,
        experiment_id = EXCLUDED.experiment_id,
        rationale = EXCLUDED.rationale,
        applied_at = NOW()
    RETURNING component"""
    result = _sql(sql)
    if result:
        print(f"  Adopted: {eid} → {component}")
    else:
        print(f"  ERROR: failed to adopt {eid} → {component}")
