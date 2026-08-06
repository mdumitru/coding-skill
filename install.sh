#!/bin/sh
# Install the shared Claude Code / Codex skills from this repo.
#
# This repo is the source of truth. Skills are COPIED into each harness rather
# than symlinked, so editing this repo changes nothing until install.sh is run
# again. "--check" reports installed copies that drifted from the repo.
#
# Principles, following ~/dotfiles/install.sh:
#   - pure POSIX shell for portability
#   - nothing is destroyed without a backup first
#   - idempotent: a second run changes nothing and creates no new backup

set -u

SCRIPT_NAME=$(basename -- "$0")
REPO_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
SKILLS_DIR="$REPO_DIR/skills"
INSTRUCTIONS_FILE="$REPO_DIR/shared/instructions.md"
BACKUP_ROOT="$HOME/coding-skill_backups"
BACKUP_DIR="$BACKUP_ROOT/$(date '+%y%m%d_%H%M%S')"
BEGIN_MARKER="<!-- BEGIN coding-skill -->"
END_MARKER="<!-- END coding-skill -->"
ALL_TARGETS="claude codex"

MODE="install"
BACKUP=1
DRY_RUN=0
TARGETS=""
DRIFT=0
BACKUP_MADE=0

show_help() {
    cat << __EOF__
Install the shared Claude Code / Codex skills from this repo.

Skills are copied, not symlinked: after editing a skill here, re-run this
script to make the change take effect.

Usage: $SCRIPT_NAME [options]

Options:
    -h, --help
        display this help message

    -t, --target <claude|codex>
        install only into this harness; repeatable. Default: both.

    --check
        report drift between this repo and the installed copies, without
        writing anything. Exits non-zero when anything differs.

    --uninstall
        remove the installed skills and the managed instructions block

    --dry-run
        print what would happen, change nothing

    -n, --no-backup
        do not back up files that are replaced; the backup is made by default
        into "$BACKUP_ROOT/<timestamp>/"

Installed locations:
    claude   \$HOME/.claude/skills/<skill>          + \$HOME/.claude/CLAUDE.md
    codex    \$CODEX_HOME/skills/<skill>            + \$CODEX_HOME/AGENTS.md
             (\$CODEX_HOME defaults to \$HOME/.codex)

The instructions files are not overwritten wholesale once managed: the body of
"shared/instructions.md" is kept between the markers
"$BEGIN_MARKER" and "$END_MARKER",
and anything outside them is preserved.
__EOF__
}

die() {
    echo "$SCRIPT_NAME: $*" >&2
    exit 1
}

info() {
    echo "$*"
}

target_home() {
    case "$1" in
        claude) echo "$HOME/.claude" ;;
        codex) echo "${CODEX_HOME:-$HOME/.codex}" ;;
    esac
}

target_instructions_name() {
    case "$1" in
        claude) echo "CLAUDE.md" ;;
        codex) echo "AGENTS.md" ;;
    esac
}

# Every directory under skills/ that actually holds a SKILL.md. Adding a skill
# needs no change to this script.
list_skills() {
    for dir in "$SKILLS_DIR"/*/; do
        test -f "$dir/SKILL.md" || continue
        basename -- "$dir"
    done
}

ensure_backup_dir() {
    test -d "$BACKUP_DIR" && return 0
    mkdir -p "$BACKUP_DIR" || die "cannot create backup directory \"$BACKUP_DIR\""
}

# Copy a path into the timestamped backup directory, keeping its position
# relative to $HOME. A no-op when the path does not exist.
backup_path() {
    test -e "$1" || return 0

    if test "$BACKUP" -eq 0; then
        info "    not backing up \"$1\" (-n given)"
        return 0
    fi

    backup_rel=${1#"$HOME"/}
    backup_dst="$BACKUP_DIR/$backup_rel"

    if test "$DRY_RUN" -eq 1; then
        info "    would back up \"$1\" -> \"$backup_dst\""
        return 0
    fi

    ensure_backup_dir
    mkdir -p "$(dirname -- "$backup_dst")" \
        || die "cannot create \"$(dirname -- "$backup_dst")\""
    cp -R -- "$1" "$backup_dst" || die "cannot back up \"$1\""
    BACKUP_MADE=1
    info "    backed up to \"$backup_dst\""
}

trees_identical() {
    diff -r -q -- "$1" "$2" > /dev/null 2>&1
}

# Print the body currently stored between the managed markers.
block_body() {
    awk -v begin_marker="$BEGIN_MARKER" -v end_marker="$END_MARKER" '
        $0 == begin_marker { in_block = 1; next }
        $0 == end_marker   { in_block = 0; next }
        in_block           { print }
    ' "$1"
}

has_block() {
    grep -qxF -- "$BEGIN_MARKER" "$1" 2> /dev/null
}

block_is_current() {
    test -f "$1" || return 1
    has_block "$1" || return 1
    block_body "$1" | diff -q -- - "$INSTRUCTIONS_FILE" > /dev/null 2>&1
}

write_block_only() {
    {
        printf '%s\n' "$BEGIN_MARKER"
        cat -- "$INSTRUCTIONS_FILE"
        printf '%s\n' "$END_MARKER"
    } > "$1" || die "cannot write \"$1\""
}

replace_block() {
    replace_tmp="${TMPDIR:-/tmp}/coding-skill.$$"
    awk -v begin_marker="$BEGIN_MARKER" -v end_marker="$END_MARKER" \
        -v body_file="$INSTRUCTIONS_FILE" '
        $0 == begin_marker {
            in_block = 1
            print begin_marker
            while ((getline body_line < body_file) > 0) print body_line
            close(body_file)
            print end_marker
            next
        }
        $0 == end_marker { in_block = 0; next }
        in_block         { next }
        { print }
    ' "$1" > "$replace_tmp" || die "cannot rewrite \"$1\""
    mv -- "$replace_tmp" "$1" || die "cannot replace \"$1\""
}

strip_block() {
    strip_tmp="${TMPDIR:-/tmp}/coding-skill.$$"
    awk -v begin_marker="$BEGIN_MARKER" -v end_marker="$END_MARKER" '
        $0 == begin_marker { in_block = 1; next }
        $0 == end_marker   { in_block = 0; next }
        in_block           { next }
        { print }
    ' "$1" > "$strip_tmp" || die "cannot rewrite \"$1\""
    mv -- "$strip_tmp" "$1" || die "cannot replace \"$1\""
}

file_is_blank() {
    test -z "$(tr -d ' \t\n' < "$1")"
}

install_skill() {
    install_src="$SKILLS_DIR/$2"
    install_dst="$1/$2"

    if trees_identical "$install_src" "$install_dst"; then
        info "  $2: up to date"
        return 0
    fi

    if test "$DRY_RUN" -eq 1; then
        info "  $2: would install into \"$install_dst\""
        backup_path "$install_dst"
        return 0
    fi

    backup_path "$install_dst"
    # A clean replace, so files deleted from the repo also disappear here.
    rm -rf -- "$install_dst" || die "cannot remove \"$install_dst\""
    cp -R -- "$install_src" "$install_dst" \
        || die "cannot copy \"$install_src\" -> \"$install_dst\""
    info "  $2: installed"
}

install_instructions() {
    if block_is_current "$1"; then
        info "  $(basename -- "$1"): up to date"
        return 0
    fi

    if test -f "$1" && has_block "$1"; then
        if test "$DRY_RUN" -eq 1; then
            info "  $(basename -- "$1"): would refresh the managed block"
            return 0
        fi
        backup_path "$1"
        replace_block "$1"
        info "  $(basename -- "$1"): managed block refreshed"
        return 0
    fi

    if test -f "$1" && ! file_is_blank "$1"; then
        # Unmanaged content: replacing it needs a backup, no exceptions.
        if test "$BACKUP" -eq 0; then
            die "refusing to replace unmanaged \"$1\" with -n; drop -n and retry"
        fi
        if test "$DRY_RUN" -eq 1; then
            info "  $(basename -- "$1"): would replace unmanaged content"
            backup_path "$1"
            return 0
        fi
        info "  $(basename -- "$1"): replacing unmanaged content"
        backup_path "$1"
        write_block_only "$1"
        info "    keep anything you still need from the backup above"
        return 0
    fi

    if test "$DRY_RUN" -eq 1; then
        info "  $(basename -- "$1"): would create with the managed block"
        return 0
    fi
    write_block_only "$1"
    info "  $(basename -- "$1"): created"
}

check_skill() {
    check_src="$SKILLS_DIR/$2"
    check_dst="$1/$2"

    if test ! -e "$check_dst"; then
        info "  $2: MISSING"
        DRIFT=1
        return 0
    fi

    if test -L "$check_dst"; then
        info "  $2: is a symlink, expected a copy"
        DRIFT=1
    fi

    check_diff=$(diff -r -- "$check_src" "$check_dst" 2>&1)
    if test -n "$check_diff"; then
        info "  $2: DRIFTED"
        echo "$check_diff" | sed 's/^/    /'
        DRIFT=1
    else
        info "  $2: ok"
    fi
}

check_instructions() {
    if test ! -f "$1"; then
        info "  $(basename -- "$1"): MISSING"
        DRIFT=1
        return 0
    fi

    if ! has_block "$1"; then
        info "  $(basename -- "$1"): no managed block"
        DRIFT=1
        return 0
    fi

    if block_is_current "$1"; then
        info "  $(basename -- "$1"): ok"
    else
        info "  $(basename -- "$1"): managed block DRIFTED"
        DRIFT=1
    fi
}

uninstall_skill() {
    uninstall_dst="$1/$2"
    test -e "$uninstall_dst" || return 0

    if test "$DRY_RUN" -eq 1; then
        info "  $2: would remove \"$uninstall_dst\""
        return 0
    fi

    if ! trees_identical "$SKILLS_DIR/$2" "$uninstall_dst"; then
        info "  $2: differs from the repo, backing it up before removal"
        backup_path "$uninstall_dst"
    fi
    rm -rf -- "$uninstall_dst" || die "cannot remove \"$uninstall_dst\""
    info "  $2: removed"
}

uninstall_instructions() {
    test -f "$1" || return 0
    has_block "$1" || return 0

    if test "$DRY_RUN" -eq 1; then
        info "  $(basename -- "$1"): would remove the managed block"
        return 0
    fi

    backup_path "$1"
    strip_block "$1"
    if file_is_blank "$1"; then
        rm -f -- "$1" || die "cannot remove \"$1\""
        info "  $(basename -- "$1"): removed (nothing left outside the block)"
    else
        info "  $(basename -- "$1"): managed block removed"
    fi
}

process_target() {
    target_dir=$(target_home "$1")
    instructions_path="$target_dir/$(target_instructions_name "$1")"
    target_skills_dir="$target_dir/skills"

    if test ! -d "$target_dir"; then
        echo "\"$target_dir\" not found! Nothing to do here ..." >&2
        return 0
    fi

    info "$1 ($target_dir):"

    if test "$MODE" = "install" && test ! -d "$target_skills_dir"; then
        if test "$DRY_RUN" -eq 1; then
            info "  would create \"$target_skills_dir\""
        else
            mkdir -p "$target_skills_dir" \
                || die "cannot create \"$target_skills_dir\""
        fi
    fi

    for skill in $SKILL_LIST; do
        case "$MODE" in
            install) install_skill "$target_skills_dir" "$skill" ;;
            check) check_skill "$target_skills_dir" "$skill" ;;
            uninstall) uninstall_skill "$target_skills_dir" "$skill" ;;
        esac
    done

    case "$MODE" in
        install) install_instructions "$instructions_path" ;;
        check) check_instructions "$instructions_path" ;;
        uninstall) uninstall_instructions "$instructions_path" ;;
    esac
}

while test $# -gt 0; do
    case "$1" in
        -h | --help | -\?)
            show_help
            exit 0
            ;;
        -n | --no-backup)
            BACKUP=0
            ;;
        --dry-run)
            DRY_RUN=1
            ;;
        --check)
            MODE="check"
            ;;
        --uninstall)
            MODE="uninstall"
            ;;
        -t | --target)
            shift
            test $# -gt 0 || die "\"-t\" needs a value (claude or codex)"
            case "$1" in
                claude | codex) TARGETS="$TARGETS $1" ;;
                *) die "unknown target \"$1\"; expected claude or codex" ;;
            esac
            ;;
        *)
            die "unknown option \"$1\"; try \"$SCRIPT_NAME --help\""
            ;;
    esac
    shift
done

test -d "$SKILLS_DIR" || die "no skills directory at \"$SKILLS_DIR\""
test -f "$INSTRUCTIONS_FILE" || die "no instructions file at \"$INSTRUCTIONS_FILE\""

SKILL_LIST=$(list_skills)
test -n "$SKILL_LIST" || die "no skills found under \"$SKILLS_DIR\""

test -n "$TARGETS" || TARGETS="$ALL_TARGETS"

for target in $TARGETS; do
    process_target "$target"
done

if test "$MODE" = "check"; then
    if test "$DRIFT" -eq 0; then
        info "everything matches this repo."
    else
        info "drift found; re-run \"$SCRIPT_NAME\" to reinstall."
    fi
    exit "$DRIFT"
fi

if test "$MODE" = "install" && test "$DRY_RUN" -eq 0; then
    info "done. Re-run this script after every change to the repo."
    test "$BACKUP_MADE" -eq 1 && info "backup: \"$BACKUP_DIR\""
fi

exit 0
