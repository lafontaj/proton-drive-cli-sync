"""Start the file-manager status bus for the installed mappings file.

With no path, the mappings file is the one stored in the engine unit. If that
unit is missing, or the file is not there, this process exits without owning
the bus name. It does not call the Proton CLI and does not change files.
"""

import argparse
import os
import sys

import cloudproviders


def main(argv):
    parser = argparse.ArgumentParser(
        description="Export sync-database status for file managers.")
    parser.add_argument("mappings", nargs="?")
    parser.add_argument("--data-dir", default=None)
    args = parser.parse_args(argv)
    path = args.mappings
    if not path:
        import schedule_manager
        path = schedule_manager.read_service_mappings_path()
    if not path or not os.path.isfile(path):
        return 0
    forwarded = ["--mappings", path]
    if args.data_dir:
        forwarded.extend(["--data-dir", args.data_dir])
    return cloudproviders.main(forwarded)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
