#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
hermes_source="${HERMES_SOURCE_ROOT:-${HOME}/.hermes/hermes-agent}"
python_bin="${HERMES_PYTHON:-${hermes_source}/venv/bin/python3}"
python_bin="$(command -v -- "${python_bin}")" || {
    printf 'Python executable not found; set HERMES_PYTHON.\n' >&2
    exit 2
}

if [[ ! -x "${python_bin}" || ! -d "${hermes_source}/gateway" ]]; then
    printf 'Hermes venv/source layout not found; set HERMES_SOURCE_ROOT or HERMES_PYTHON.\n' >&2
    exit 2
fi

created_profile_dir=""
if [[ -z "${HERMES_PROFILE_DIR:-}" ]]; then
    created_profile_dir="$(mktemp -d "${TMPDIR:-/tmp}/meshcore-tests.XXXXXX")"
    export HERMES_PROFILE_DIR="${created_profile_dir}"
fi
trap 'if [[ -n "${created_profile_dir}" ]]; then rm -rf "${created_profile_dir}"; fi' EXIT

export PYTHONPATH="${repo_dir}:${hermes_source}${PYTHONPATH:+:${PYTHONPATH}}"
cd "${repo_dir}"
"${python_bin}" run_tests.py
