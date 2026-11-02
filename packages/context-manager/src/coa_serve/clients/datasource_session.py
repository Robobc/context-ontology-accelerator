# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Refreshable STS AssumeRole for customer-supplied cross-account datasource roles (fork).

Serve is a long-lived process: a Knowledge Base in a customer account is read on
every request for as long as the namespace exists, so the assumed credentials
must renew themselves. This is the long-lived counterpart of
``coa_sources.database.connectors.sts_assume.assume_datasource_session`` (the
one-shot form the discovery Lambda and the kg-build task use) and keeps its one
load-bearing rule: the **ExternalId is required**. The role ARN arrived from the
``CreateSource`` caller; the ExternalId, derived from the namespace and never
from a request, is what binds the assume to the namespace entitled to it. A
caller holding ``manageSource`` on one namespace therefore cannot register
another tenant's ``*-datasource-access-*`` role and read that tenant's
documents through this deployment (confused deputy).
"""

from __future__ import annotations

from collections.abc import Callable

import boto3
import botocore.session
import structlog
from botocore.credentials import RefreshableCredentials
from botocore.exceptions import ClientError
from coa_common.aws_config import sync_boto_config

logger = structlog.get_logger(__name__)

# STS caps RoleSessionName at 64 characters.
MAX_SESSION_NAME_LEN = 64


class DatasourceAssumeError(ValueError):
    """``sts:AssumeRole`` on a customer-supplied datasource role was refused.

    ``code`` carries STS's own error code; the message deliberately omits the
    role ARN so a caller-facing error names no customer resource.
    """

    def __init__(self, code: str) -> None:
        """Build the caller-safe message from *code* and keep the code readable."""
        super().__init__(f"Failed to assume role: {code}")
        self.code = code


def refreshable_datasource_session(
    role_arn: str,
    external_id: str,
    region: str,
    session_name: str,
    *,
    sts_client_factory: Callable[[], object] | None = None,
) -> boto3.Session:
    """Assume *role_arn* with *external_id* and return a session whose credentials renew themselves.

    The first assume happens here, so a refused role fails at bind time (the
    admission gate reports it) rather than on the first ``Retrieve``. Later
    renewals run inside botocore when the credentials near expiry, with the same
    ExternalId. Raises ``ValueError`` when either argument is empty and
    :class:`DatasourceAssumeError` when STS refuses.

    *session_name* is truncated to the STS limit and lands in the data owner's
    CloudTrail, so callers encode the requesting namespace in it.
    """
    if not role_arn:
        raise ValueError("role_arn is required to assume a cross-account datasource role")
    if not external_id:
        raise ValueError("external_id is required to assume a cross-account datasource role")

    sts = (
        sts_client_factory()
        if sts_client_factory is not None
        else boto3.client("sts", region_name=region, config=sync_boto_config())
    )
    name = session_name[:MAX_SESSION_NAME_LEN]

    def _refresh() -> dict[str, str]:
        try:
            creds = sts.assume_role(RoleArn=role_arn, RoleSessionName=name, ExternalId=external_id)["Credentials"]
        except ClientError as e:
            # Operators get the ARN in the log; the caller gets only STS's code.
            logger.warning("datasource_assume_role_failed", role_arn=role_arn, session_name=name)
            raise DatasourceAssumeError(e.response["Error"]["Code"]) from e
        logger.info("datasource_assume_role_ok", role_arn=role_arn, session_name=name)
        return {
            "access_key": creds["AccessKeyId"],
            "secret_key": creds["SecretAccessKey"],
            "token": creds["SessionToken"],
            "expiry_time": creds["Expiration"].isoformat(),
        }

    credentials = RefreshableCredentials.create_from_metadata(
        metadata=_refresh(), refresh_using=_refresh, method="sts-assume-role"
    )
    botocore_session = botocore.session.get_session()
    botocore_session._credentials = credentials  # the documented way to seed a session with refreshable creds
    return boto3.Session(botocore_session=botocore_session, region_name=region)
