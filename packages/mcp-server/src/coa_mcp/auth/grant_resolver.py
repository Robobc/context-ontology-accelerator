# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Grant resolution — resolve user identity to InvokeRequest.profile.

This is the critical data-scoping component. Without a populated profile,
the SQL Firewall no-ops (empty profile = unrestricted access).

An empty/missing authorization object must FAIL CLOSED,
never default-allow.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import structlog
from coa_authorization.policy_evaluator import evaluate as cedar_evaluate
from coa_common import resolve_region

from .claims import CallerIdentity

logger = structlog.get_logger(__name__)

_ROLES_TABLE = os.environ.get("ROLES_TABLE_NAME", "")
_AWS_REGION = resolve_region()


class GrantResolutionError(Exception):
    """Raised when a user's grant cannot be resolved."""


# ── MCP tool → Cedar action mapping ──────────────────────────────────────────

TOOL_CEDAR_ACTION: dict[str, str] = {
    "list_metrics": "viewNamespace",
    "describe_schema": "viewNamespace",
    "query": "query",
    "semantic_execution_plan": "query",
    "translate_sparql": "query",
    "translate_sql": "query",
    "rag_retrieval": "searchDocuments",
    "graph_traversal": "viewNamespace",
}


def _evaluate_cedar(
    user_id: str,
    action: str,
    namespace_id: str,
    *,
    global_roles: list[str],
    resource_roles: list[dict[str, str]],
) -> None:
    """Load Cedar policies for the principal's roles and evaluate.

    Mirrors the control-plane authorizer pattern: loads cedarPolicy from the
    Roles table for each resolved role + default, then calls
    coa_authorization.policy_evaluator.evaluate().

    Raises GrantResolutionError on deny (fail-closed).
    """
    from coa_authorization.policy_loader import load_policies_for_roles

    role_ids: set[str] = {"default"}
    role_ids.update(global_roles)
    role_ids.update(r.get("role", "") for r in resource_roles if r.get("role"))

    policies = load_policies_for_roles(
        role_ids=role_ids,
        table_name=_ROLES_TABLE,
        region=_AWS_REGION,
    )

    if not policies:
        logger.warning("cedar_no_policies_loaded", user_id=user_id, roles=list(role_ids))
        raise GrantResolutionError(f"No Cedar policies found for user '{user_id}'")

    result = cedar_evaluate(
        policies=policies,
        user_id=user_id,
        action=action,
        resource_type="Namespace",
        resource_id=namespace_id,
        global_roles=global_roles,
        resource_roles=resource_roles,
        resource_attrs={"namespace": namespace_id},
    )

    if not result.allowed:
        logger.warning(
            "cedar_access_denied",
            user_id=user_id,
            action=action,
            namespace=namespace_id,
            roles=list(role_ids),
        )
        raise GrantResolutionError(
            f"Cedar policy denied action '{action}' for user '{user_id}' on namespace '{namespace_id}'"
        )


@dataclass
class ResolvedProfile:
    """The resolved data-access profile for a user in a namespace.

    This populates InvokeRequest.profile, which the SQL Firewall uses
    for table allowlist and column denylist enforcement, and the Cedar
    authorizer uses for role-based policy evaluation.
    """

    namespace: str
    user_id: str = ""
    global_roles: list[str] = field(default_factory=list)
    resource_roles: list[dict[str, str]] = field(default_factory=list)
    # None = unrestricted (no grant declares a constraint). An explicitly EMPTY
    # list/map is a real restriction (deny-all) under the SQL firewall's
    # empty-vs-absent semantics, so the two must not be collapsed here.
    table_allowlist: list[str] | None = None
    column_denylist: dict[str, list[str]] | None = None
    data_source_ids: list[str] | None = None

    def to_profile_dict(self) -> dict:
        """Convert to the dict format expected by InvokeRequest.profile.

        Restriction keys are omitted when unrestricted (None) rather than sent
        as empty lists: an empty tableAllowlist now means deny-all to the SQL
        firewall, and the serve entrypoint strips + re-resolves these fields
        server-side anyway — forwarding fabricated empties only trips its
        client_role_injection_stripped warning on every call.
        """
        profile: dict = {
            "namespace": self.namespace,
            "userId": self.user_id,
            "globalRoles": self.global_roles,
            "resourceRoles": self.resource_roles,
        }
        if self.table_allowlist is not None:
            profile["tableAllowlist"] = self.table_allowlist
        if self.column_denylist is not None:
            profile["columnDenylist"] = self.column_denylist
        if self.data_source_ids is not None:
            profile["dataSourceIds"] = self.data_source_ids
        return profile


async def resolve_grant(
    caller: CallerIdentity,
    namespace_id: str,
    *,
    cedar_action: str | None = None,
) -> ResolvedProfile:
    """Resolve the caller's grant for the given namespace.

    This function:
    1. Validates the caller has access to the requested namespace
    2. Evaluates Cedar policies if cedar_action is provided (fail-closed)
    3. Resolves their data-access profile (table allowlist, column denylist)
       via the shared role_resolver from coa_serve.

    FAIL CLOSED: If the grant cannot be resolved, access is DENIED.

    Args:
        caller: The extracted caller identity from JWT claims.
        namespace_id: The namespace being accessed.
        cedar_action: Cedar action to evaluate (e.g., "query", "viewNamespace").
            If None, Cedar evaluation is skipped (backward compat).

    Returns:
        ResolvedProfile with the caller's data-access permissions.

    Raises:
        GrantResolutionError: If the caller lacks access or grant cannot be resolved.
    """
    # ── Namespace pre-filter (advisory) ───────────────────────────────
    # Only enforced if the JWT actually carried a ``custom:namespaces`` claim.
    # An empty/absent list is NOT a denial signal: the vast majority of tokens
    # (Cognito access tokens, generic OIDC tokens) never populate that claim,
    # and denying pre-lookup here would reject every non-admin caller before
    # the RRM grant table gets a chance to speak. The authoritative access
    # check is the resolve_profile → RRM lookup below, plus the
    # namespace-scoped emptiness check further down, followed by Cedar policy
    # evaluation.
    if caller.namespaces and namespace_id not in caller.namespaces:
        logger.warning(
            "namespace_access_denied",
            user_id=caller.user_id,
            agent_id=caller.agent_id,
            requested_namespace=namespace_id,
            # Count, not the full entitlement list — the list exposes the
            # caller's entire authorization perimeter to log readers.
            allowed_namespace_count=len(caller.namespaces),
        )
        raise GrantResolutionError(f"User '{caller.user_id}' does not have access to namespace '{namespace_id}'")

    # ── Resolve data-access profile via shared role resolver ──────────
    # Pass ``email`` so grants written under the caller's email address (the
    # default when granting through the UI — Cognito's ``sub`` is a UUID users
    # never see) resolve for a caller whose JWT surfaces both ``sub`` and
    # ``email``. Skipping ``email`` here silently hides those grants and
    # denies every human user with the misleading "No grants found" error.
    try:
        from coa_serve.role_resolver import resolve_profile

        resolved = resolve_profile(
            user_id=caller.user_id,
            groups=caller.roles,
            namespace=namespace_id,
            email=caller.email,
        )
    except Exception as e:
        # Fail closed for EVERY caller, admins included.
        #
        # There used to be an admin fallback here that returned platform-admin +
        # namespace-owner and returned EARLY — before the Cedar evaluation below,
        # despite that block's comment claiming it is never bypassed. It granted
        # those roles off nothing but an "admin" entry in the caller's IdP groups,
        # with no policy check and no data restrictions.
        #
        # It was near-unreachable while resolve_profile swallowed its own query
        # errors, but that method now propagates them (a partial grant read is more
        # permissive than the truth, so degrading there widened data access). A
        # DynamoDB throttle would therefore have escalated any admin-group caller.
        #
        # A failed grant read means we do not know what this caller may do, which is
        # never a reason to assume the most privileged answer. Denying turns a
        # transient backend error into a retryable failure instead of a silent
        # privilege escalation.
        logger.error(
            "grant_resolution_failed",
            user_id=caller.user_id,
            namespace=namespace_id,
            is_admin_group_member=any(r.lower() == "admin" for r in caller.roles),
            error=str(e),
        )
        raise GrantResolutionError(f"Failed to resolve grant for user '{caller.user_id}': {e}") from e

    # ── Fail closed on empty grants for THIS namespace ────────────────
    # ``resolved.resource_roles`` accumulates grants across every namespace
    # this caller holds — ``resolve_profile`` only uses ``namespace=`` to
    # scope the data-restrictions merge, not the roles list. So the check
    # here must filter by ``resourceUID`` before the emptiness test:
    # a caller who holds ``namespace-owner`` on ``sales`` and requests ``hr``
    # would otherwise pass an "any grants at all" test with the sales grant
    # and hit Cedar with a profile that has no bearing on hr. Cedar catches
    # it today (all six MCP tools pass ``cedar_action`` and ``ROLES_TABLE_NAME``
    # is set in this deployment), but ``cedar_action`` defaults to ``None`` and
    # unset ``ROLES_TABLE_NAME`` skips Cedar entirely — this fail-closed check
    # is the last line of defense and must be namespace-scoped.
    #
    # Global roles (``GLOBAL`` resource in RRM — platform-admin, platform-viewer)
    # still pass because they grant cross-namespace access by design; the admin
    # IdP group bypass is unchanged.
    namespace_resource_roles = [r for r in resolved.resource_roles if r.get("resourceUID") == namespace_id]
    is_admin_group_member = any(r.lower() == "admin" for r in caller.roles)
    if not namespace_resource_roles and not resolved.global_roles and not is_admin_group_member:
        logger.warning(
            "grant_lookup_empty",
            user_id=caller.user_id,
            agent_id=caller.agent_id,
            requested_namespace=namespace_id,
            cross_namespace_role_count=len(resolved.resource_roles),
        )
        raise GrantResolutionError(
            f"User '{caller.user_id}' has no grants on namespace '{namespace_id}' and is not an admin"
        )

    # ── Cedar policy evaluation (OUTSIDE try/except, never bypassed) ──
    if cedar_action and _ROLES_TABLE:
        _evaluate_cedar(
            caller.user_id,
            cedar_action,
            namespace_id,
            global_roles=resolved.global_roles,
            resource_roles=resolved.resource_roles,
        )

    logger.info(
        "grant_resolved",
        user_id=caller.user_id,
        namespace=namespace_id,
        global_roles=resolved.global_roles,
        table_allowlist_count=len(resolved.table_allowlist or []),
        column_denylist_count=len(resolved.column_denylist or {}),
        data_source_ids_count=len(resolved.allowed_metrics or []),
    )

    return ResolvedProfile(
        namespace=namespace_id,
        user_id=caller.user_id,
        global_roles=resolved.global_roles,
        resource_roles=resolved.resource_roles,
        # Pass through as-is: None (unrestricted) must not become [] — an empty
        # list is a deny-all restriction under the firewall's semantics.
        table_allowlist=resolved.table_allowlist,
        column_denylist=resolved.column_denylist,
        data_source_ids=resolved.allowed_metrics,
    )
