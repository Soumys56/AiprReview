
from unidiff import PatchSet

def separate_before_after(patch: str, filename: str = "file.py"):

    if not patch:
        return {
            "before_changes": "",
            "after_changes": ""
        }
    # Add Git diff headers
    if not patch.startswith("diff --git"):
        patch = (
            f"diff --git a/{filename} b/{filename}\n"
            f"--- a/{filename}\n"
            f"+++ b/{filename}\n"
            + patch
        )
    patch_set = PatchSet(patch)
    before_changes = []
    after_changes = []
    for patched_file in patch_set:
        for hunk in patched_file:
            for line in hunk:
                if line.is_removed:
                    before_changes.append(line.value.rstrip("\n"))

                elif line.is_added:
                    after_changes.append(line.value.rstrip("\n"))

                elif line.is_context:
                    before_changes.append(line.value.rstrip("\n"))
                    after_changes.append(line.value.rstrip("\n"))
    return {
        "before_changes": "\n".join(before_changes),
        "after_changes": "\n".join(after_changes)
    }
