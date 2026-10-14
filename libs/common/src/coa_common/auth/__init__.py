# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Authentication utilities — token authorizer ABC and implementations."""

from coa_common.auth.cognito_authorizer import CognitoTokenAuthorizer
from coa_common.auth.factory import build_token_authorizer, is_cognito_issuer
from coa_common.auth.oidc_authorizer import OIDCTokenAuthorizer
from coa_common.auth.token_authorizer import (
    TokenAuthorizer,
    TokenType,
    TokenValidationResult,
    extract_email_claim,
)

__all__ = [
    "CognitoTokenAuthorizer",
    "OIDCTokenAuthorizer",
    "TokenAuthorizer",
    "TokenType",
    "TokenValidationResult",
    "build_token_authorizer",
    "extract_email_claim",
    "is_cognito_issuer",
]
