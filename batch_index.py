import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tqdm import tqdm

# --- 统一配置 ---
sys.path.insert(0, str(Path(__file__).parent.resolve()))
from settings import settings
from indexing import get_result_path, is_already_indexed, run_index_file

NOTES_DIR   = settings.vault_path
RESULTS_DIR = settings.results_dir
MAX_WORKERS = settings.max_workers


def process_file(md_path_str: str):
    md_path     = Path(md_path_str)
    result_path = get_result_path(md_path)

    if is_already_indexed(result_path):
        return ("Skipped", md_path_str)

    status = run_index_file(md_path)
    if status == 'ok':
        return ("Success", md_path_str)
    elif status == 'timeout':
        return ("Error", f"{md_path_str}\ntimeout")
    else:
        return ("Error" if status.startswith('error') else "Exception",
                f"{md_path_str}\n{status}")


def main():
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    import os
    md_files = []
    for root, dirs, files in os.walk(str(NOTES_DIR)):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for file in files:
            if file.endswith(".md"):
                md_files.append(str(Path(root) / file))

    print(f"Found {len(md_files)} Markdown files. Resuming from last progress...")

    to_process = []
    skipped_count = 0
    for f in md_files:
        if is_already_indexed(get_result_path(Path(f))):
            skipped_count += 1
        else:
            to_process.append(f)

    print(f"Already Indexed: {skipped_count}")
    print(f"To Process: {len(to_process)}")

    if not to_process:
        print("All files are already indexed.")
        return

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        results = list(tqdm(executor.map(process_file, to_process), total=len(to_process)))

    success_count = sum(1 for status, _ in results if status == "Success")
    error_results = [msg for status, msg in results if status in ["Error", "Exception"]]

    print("\nBatch Processing Summary:")
    print(f"New Successes: {success_count}")
    print(f"New Errors: {len(error_results)}")

    if error_results:
        print("\nDetail of errors:")
        for err in error_results[:10]:
            print(f"- {err}")
        if len(error_results) > 10:
            print(f"... and {len(error_results) - 10} more errors.")


if __name__ == "__main__":
    main()
