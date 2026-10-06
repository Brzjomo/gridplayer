#!/bin/bash

set -e

SCRIPT_DIR="$( cd "$( dirname $0 )" && pwd )"

. "scripts/init_app_vars.sh"

make_requirements
