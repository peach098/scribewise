"""Jira task source for Jira Cloud and self-hosted Jira (specs/003-jira-commitments).

- ``source``: :class:`JiraTaskSource` (the ``TaskSource`` protocol; issues as tasks with
  comments, description and due changes, research J7);
- ``client`` / ``flavors``: the GET-only HTTP client and the Cloud / self-hosted differences
  (research J1–J3);
- ``bodies``: comment bodies (ADF on Cloud, wiki markup on self-hosted) as plain text;
- ``netguard`` / ``tls``: private-address guard and certificate pinning (research J4, J5);
- ``jql``: JQL builders; ``sync``: the comment sync jobs (research J8 – J11).
"""

from scribe.context.tasks.jira.source import JiraTaskSource

__all__ = ["JiraTaskSource"]
