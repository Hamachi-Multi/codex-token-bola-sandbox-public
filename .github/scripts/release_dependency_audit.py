#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sys
from typing import Any


GHSA_PATTERN = re.compile(r"^GHSA-[23456789cfghjmpqrvwxy]{4}-[23456789cfghjmpqrvwxy]{4}-[23456789cfghjmpqrvwxy]{4}$", re.IGNORECASE)
ALLOWED_SEVERITIES = {"low", "moderate", "high", "critical"}


class InputError(ValueError):
    pass


def read_object(path: pathlib.Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise InputError(f"cannot read {label}: {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise InputError(f"invalid {label} json: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise InputError(f"{label} must be a JSON object")
    return payload


def string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value or any(not isinstance(item, str) or not item for item in value):
        raise InputError(f"{label} must be a non-empty string list")
    if len(set(value)) != len(value):
        raise InputError(f"{label} must not contain duplicates")
    return value


def parse_allowlist(payload: dict[str, Any], today: dt.date) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, dict[str, Any]], str]:
    expected_keys = {"schema_version", "expires_on", "allowed_node_prefix", "advisories", "meta_vulnerabilities"}
    if set(payload) != expected_keys:
        raise InputError(f"allowlist keys must be exactly {sorted(expected_keys)}")
    if payload.get("schema_version") != 1:
        raise InputError("allowlist schema_version must be 1")
    try:
        expires_on = dt.date.fromisoformat(str(payload.get("expires_on") or ""))
    except ValueError as exc:
        raise InputError("allowlist expires_on must be an ISO date") from exc
    if today > expires_on:
        raise InputError(f"allowlist expired on {expires_on.isoformat()}")
    prefix = payload.get("allowed_node_prefix")
    if not isinstance(prefix, str) or not prefix.startswith("node_modules/") or not prefix.endswith("/"):
        raise InputError("allowed_node_prefix must be a node_modules path prefix")

    advisories: dict[tuple[str, str], dict[str, Any]] = {}
    entries = payload.get("advisories")
    if not isinstance(entries, list) or not entries:
        raise InputError("allowlist advisories must be a non-empty list")
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != {"id", "package", "severity", "nodes"}:
            raise InputError(f"advisories[{index}] has invalid keys")
        advisory_id = str(entry.get("id") or "")
        package = str(entry.get("package") or "")
        severity = str(entry.get("severity") or "")
        nodes = string_list(entry.get("nodes"), f"advisories[{index}].nodes")
        if not GHSA_PATTERN.fullmatch(advisory_id):
            raise InputError(f"advisories[{index}].id must be a GHSA identifier")
        if not package:
            raise InputError(f"advisories[{index}].package is required")
        if severity not in ALLOWED_SEVERITIES:
            raise InputError(f"advisories[{index}].severity is invalid")
        if any(not node.startswith(prefix) for node in nodes):
            raise InputError(f"advisories[{index}].nodes must stay under {prefix}")
        key = (advisory_id.upper(), package)
        if key in advisories:
            raise InputError(f"duplicate advisory exception: {advisory_id} for {package}")
        advisories[key] = {"severity": severity, "nodes": nodes}

    meta: dict[str, dict[str, Any]] = {}
    meta_entries = payload.get("meta_vulnerabilities")
    if not isinstance(meta_entries, list) or not meta_entries:
        raise InputError("allowlist meta_vulnerabilities must be a non-empty list")
    for index, entry in enumerate(meta_entries):
        if not isinstance(entry, dict) or set(entry) != {"package", "severity", "nodes"}:
            raise InputError(f"meta_vulnerabilities[{index}] has invalid keys")
        package = str(entry.get("package") or "")
        severity = str(entry.get("severity") or "")
        nodes = string_list(entry.get("nodes"), f"meta_vulnerabilities[{index}].nodes")
        if not package or package in meta:
            raise InputError(f"invalid or duplicate meta vulnerability package: {package}")
        if severity not in ALLOWED_SEVERITIES:
            raise InputError(f"meta_vulnerabilities[{index}].severity is invalid")
        meta[package] = {"severity": severity, "nodes": nodes}
    return advisories, meta, expires_on.isoformat()


def advisory_id(via: dict[str, Any]) -> str:
    url = str(via.get("url") or "")
    candidate = url.rstrip("/").rsplit("/", 1)[-1].upper()
    if not GHSA_PATTERN.fullmatch(candidate):
        raise InputError(f"audit advisory URL does not end in a GHSA identifier: {url or '<missing>'}")
    return candidate


def vulnerability_nodes(vulnerability: dict[str, Any], label: str) -> list[str]:
    return string_list(vulnerability.get("nodes"), f"audit vulnerability {label}.nodes")


def configured_plugins(release_config: dict[str, Any]) -> list[str]:
    plugins = release_config.get("plugins")
    if not isinstance(plugins, list):
        raise InputError("release config plugins must be a list")
    result: list[str] = []
    for index, entry in enumerate(plugins):
        if isinstance(entry, str):
            result.append(entry)
        elif isinstance(entry, list) and entry and isinstance(entry[0], str):
            result.append(entry[0])
        else:
            raise InputError(f"release config plugins[{index}] is invalid")
    return result


def validate_audit(audit: dict[str, Any], allowlist: dict[str, Any], release_config: dict[str, Any], today: dt.date) -> dict[str, Any]:
    advisories, meta, expires_on = parse_allowlist(allowlist, today)
    if "@semantic-release/npm" in configured_plugins(release_config):
        raise InputError("@semantic-release/npm must remain disabled while its bundled dependencies are allowlisted")
    if audit.get("auditReportVersion") != 2:
        raise InputError("npm audit report version must be 2")
    vulnerabilities = audit.get("vulnerabilities")
    metadata = audit.get("metadata")
    if not isinstance(vulnerabilities, dict) or not isinstance(metadata, dict):
        raise InputError("npm audit report must contain vulnerabilities and metadata objects")

    errors: list[str] = []
    used_advisories: set[tuple[str, str]] = set()
    used_meta: set[str] = set()
    resolving: set[str] = set()
    resolved: dict[str, set[tuple[str, str]]] = {}

    def resolve(package: str) -> set[tuple[str, str]]:
        if package in resolved:
            return resolved[package]
        if package in resolving:
            raise InputError(f"npm audit vulnerability cycle at {package}")
        vulnerability = vulnerabilities.get(package)
        if not isinstance(vulnerability, dict):
            raise InputError(f"npm audit references missing vulnerability: {package}")
        via = vulnerability.get("via")
        if not isinstance(via, list) or not via:
            raise InputError(f"npm audit vulnerability has no via chain: {package}")
        resolving.add(package)
        leaves: set[tuple[str, str]] = set()
        for source in via:
            if isinstance(source, str):
                leaves.update(resolve(source))
            elif isinstance(source, dict):
                leaves.add((advisory_id(source), package))
            else:
                raise InputError(f"npm audit vulnerability has invalid via entry: {package}")
        resolving.remove(package)
        if not leaves:
            raise InputError(f"npm audit vulnerability resolves to no advisories: {package}")
        resolved[package] = leaves
        return leaves

    for package, raw_vulnerability in vulnerabilities.items():
        if not isinstance(package, str) or not isinstance(raw_vulnerability, dict):
            errors.append("npm audit vulnerabilities must map package names to objects")
            continue
        severity = str(raw_vulnerability.get("severity") or "")
        nodes = vulnerability_nodes(raw_vulnerability, package)
        via = raw_vulnerability.get("via")
        has_meta_source = isinstance(via, list) and any(isinstance(source, str) for source in via)
        try:
            leaves = resolve(package)
        except InputError as exc:
            errors.append(str(exc))
            continue
        if has_meta_source:
            expected = meta.get(package)
            if expected is None:
                errors.append(f"unapproved meta vulnerability: {package}")
            elif severity != expected["severity"] or nodes != expected["nodes"]:
                errors.append(f"meta vulnerability contract changed: {package}")
            else:
                used_meta.add(package)
        for key in leaves:
            expected = advisories.get(key)
            if expected is None:
                errors.append(f"unapproved advisory: {key[0]} for {key[1]}")
                continue
            leaf = vulnerabilities.get(key[1])
            if not isinstance(leaf, dict):
                errors.append(f"missing leaf vulnerability: {key[1]}")
                continue
            leaf_nodes = vulnerability_nodes(leaf, key[1])
            via_entries = leaf.get("via") if isinstance(leaf.get("via"), list) else []
            matching_sources = [source for source in via_entries if isinstance(source, dict) and advisory_id(source) == key[0]]
            leaf_severity = str(matching_sources[0].get("severity") or "") if matching_sources else ""
            if leaf_severity != expected["severity"] or leaf_nodes != expected["nodes"]:
                errors.append(f"advisory contract changed: {key[0]} for {key[1]}")
                continue
            used_advisories.add(key)

    unused_advisories = sorted(set(advisories) - used_advisories)
    unused_meta = sorted(set(meta) - used_meta)
    errors.extend(f"unused advisory exception: {advisory_id_value} for {package}" for advisory_id_value, package in unused_advisories)
    errors.extend(f"unused meta vulnerability exception: {package}" for package in unused_meta)
    return {
        "ok": not errors,
        "errors": errors,
        "expires_on": expires_on,
        "allowed_advisories": len(used_advisories),
        "allowed_vulnerabilities": len(vulnerabilities) if not errors else 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate npm audit output against a narrow release-only exception")
    parser.add_argument("--audit-json", required=True, type=pathlib.Path)
    parser.add_argument("--allowlist", required=True, type=pathlib.Path)
    parser.add_argument("--release-config", required=True, type=pathlib.Path)
    parser.add_argument("--today", type=dt.date.fromisoformat)
    args = parser.parse_args(argv)
    try:
        result = validate_audit(
            read_object(args.audit_json, "npm audit report"),
            read_object(args.allowlist, "audit allowlist"),
            read_object(args.release_config, "semantic-release config"),
            args.today or dt.datetime.now(dt.timezone.utc).date(),
        )
    except InputError as exc:
        result = {"ok": False, "errors": [str(exc)]}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
