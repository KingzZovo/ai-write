"""Chapter-side scaling: cheap index wins instead of premature partitioning.

Decision record (measured 2026-07-26, live DB):

    chapters          872 rows, 18 MB total (3 MB heap — prose lives in
                      TOAST). Even a 千万字-scale project is only tens of
                      thousands of rows; btree indexes cover that with
                      room to spare. Partitioning now would buy nothing
                      and cost PK/FK complexity (9 tables reference
                      chapters.id — a composite partition-key PK would
                      ripple through all of them).
    chapter_versions  116 rows, 13 MB. Same verdict. Growth is bounded by
                      authoring activity, and the keep-last-K retention
                      task (tasks.enforce_chapter_version_retention,
                      default keep-all) caps it if it ever matters.

Trigger point (recorded so the decision is revisited, not forgotten):
partition `chapters` (by volume_id hash or project via composite key)
only when rows exceed ~500k OR heap (pg_relation_size, not TOAST)
exceeds ~5 GB OR p95 of the context-pack window query degrades despite
the indexes below. At the current 872 rows we are 3 orders of magnitude
away.

What this migration DOES add — indexes matched to real query shapes:

    * ix_chapters_volume_chapter_idx (volume_id, chapter_idx):
      context_pack.py windows chapters with
      `WHERE volume_id = :v AND chapter_idx BETWEEN ... ORDER BY
      chapter_idx` and the volume_id FK had NO index at all (every
      lookup was a seq scan; also makes volume CASCADE deletes cheap).
    * ix_chapters_global_idx_summarized — partial index for
      context_pack.py recent-summaries:
      `WHERE summary IS NOT NULL AND summary <> '' AND global_idx IS NOT
      NULL AND global_idx <= :g ORDER BY global_idx`.
    * ix_chapter_versions_chapter_created (chapter_id, created_at):
      chapter_versions.chapter_id is an un-indexed FK; serves the
      version-tree listing, chapter CASCADE deletes, and the keep-last-K
      retention selection.

Locks: plain CREATE INDEX (not CONCURRENTLY — we are inside the alembic
transaction) takes SHARE, blocking writes on these tables for the build.
At 872 / 116 rows that is milliseconds.
"""

from alembic import op

revision = "a1001917"
down_revision = "a1001916"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_chapters_volume_chapter_idx
        ON chapters (volume_id, chapter_idx);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_chapters_global_idx_summarized
        ON chapters (global_idx)
        WHERE summary IS NOT NULL AND summary <> '' AND global_idx IS NOT NULL;
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_chapter_versions_chapter_created
        ON chapter_versions (chapter_id, created_at);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chapter_versions_chapter_created;")
    op.execute("DROP INDEX IF EXISTS ix_chapters_global_idx_summarized;")
    op.execute("DROP INDEX IF EXISTS ix_chapters_volume_chapter_idx;")
