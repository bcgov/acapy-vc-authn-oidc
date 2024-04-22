import canonicaljson
import dataclasses
import json
from datetime import datetime
from typing import Any, Dict, List

import structlog
from oic.oic.message import OpenIDSchema
from pydantic import BaseModel

from ...authSessions.models import AuthSession
from ...verificationConfigs.models import ReqAttr, VerificationConfig
from ...core.models import RevealedAttribute

logger = structlog.getLogger(__name__)

PROOF_CLAIMS_ATTRIBUTE_NAME = "vc_presented_attributes"


@dataclasses.dataclass(frozen=True)
class Claim(BaseModel):
    type: str
    value: str


class Token(BaseModel):
    creation_time: datetime = datetime.now()
    issuer: str
    audiences: List[str]
    lifetime: int
    claims: Dict[str, Any]

    @classmethod
    def get_claims(
        cls, auth_session: AuthSession, ver_config: VerificationConfig
    ) -> dict[str, str]:
        """Converts vc presentation values to oidc claims"""
        oidc_claims: List[Claim] = [
            Claim(
                type="pres_req_conf_id",
                value=auth_session.request_parameters["pres_req_conf_id"],
            ),
            Claim(type="acr", value="vc_authn"),
        ]
        # subject claim

        oidc_claims.append(
            Claim(type="nonce", value=auth_session.request_parameters["nonce"])
        )

        presentation_claims: Dict[str, Claim] = {}
        logger.info(
            auth_session.presentation_exchange["presentation_request"][
                "requested_attributes"
            ]
        )

        referent: str
        requested_attr: ReqAttr
        try:
            for referent, requested_attrdict in auth_session.presentation_exchange[
                "presentation_request"
            ]["requested_attributes"].items():
                requested_attr = ReqAttr(**requested_attrdict)
                logger.debug(
                    f"Processing referent: {referent}, requested_attr: {requested_attr}"
                )
                revealed_attrs: Dict[str, RevealedAttribute] = (
                    auth_session.presentation_exchange["presentation"][
                        "requested_proof"
                    ]["revealed_attr_groups"]
                )
                logger.debug(f"revealed_attrs: {revealed_attrs}")
                # loop through each value and put it in token as a claim
                for attr_name in requested_attr.names:
                    logger.debug(f"AttrName: {attr_name}")
                    presentation_claims[attr_name] = Claim(
                        type=attr_name,
                        value=revealed_attrs[referent]["values"][attr_name]["raw"],
                    )
                    logger.debug(f"Compiled presentation_claims: {presentation_claims}")
        except Exception as err:
            logger.error(
                f"An exception occurred while extracting the proof claims: {err}"
            )
            raise RuntimeError(err)

        proof_claims = json.dumps(
            {c.type: c.value for c in presentation_claims.values()}
        )
        # look at all presentation_claims for one
        # matching the configured subject_identifier, if any
        sub_id_claim = presentation_claims.get(ver_config.subject_identifier)

        if sub_id_claim:
            # add sub and append presentation_claims
            oidc_claims.append(Claim(type="sub", value=sub_id_claim.value))
        elif ver_config.generate_consistent_identifier:
            # Do not create a sub based on the proof claims if the
            # user requests a generated identifier
            oidc_claims.append(
                Claim(
                    type="sub",
                    value=canonicaljson.encode_canonical_json(proof_claims).decode(
                        "utf-8"
                    ),
                )
            )

        result = {c.type: c.value for c in oidc_claims}
        result[PROOF_CLAIMS_ATTRIBUTE_NAME] = proof_claims

        # TODO: Remove after full transistion to v2.0
        # Add the presentation claims to the result as keys
        # for backwards compatibility [v1.0]
        if ver_config.include_v1_attributes:
            for key, value in presentation_claims.items():
                result[key] = value.value

        return result

    # TODO: Determine if this is useful to keep, and remove it if it's not.
    # It is currently unused.
    # renames and calculates dict members appropriate to
    # https://openid.net/specs/openid-connect-core-1_0.html#IDToken
    # and
    # https://github.com/OpenIDC/pyoidc/blob/26ea5121239dad03c5c5551cca149cb984df1ec9/src/oic/oic/message.py#L720
    def idtoken_dict(self, nonce: str) -> Dict:
        """Converts oidc claims to IdToken attribute names"""

        result = {}  # nest VC attribute claims under the key=pres_req_conf_id

        # for type, value in self.claims.items():
        #     result[type] = value

        result["exp"] = int(round(datetime.now().timestamp())) + self.lifetime
        result["aud"] = self.audiences
        result["nonce"] = nonce

        result.update(self.claims)

        # identify if any standardclaims were provided in the proof and return
        # them at the top level.
        # https://openid.net/specs/openid-connect-core-1_0.html#StandardClaims

        # make copy of dict
        r2 = result.copy()
        # add nested values to top level
        r2.update(json.loads(self.claims[PROOF_CLAIMS_ATTRIBUTE_NAME]))
        # only keep ones that match the OpenIDschema
        r2 = {
            key: r2[key]
            for key in set(r2.keys()).intersection(set(OpenIDSchema().c_param.keys()))
        }

        # verify with library schema
        standard_claims = OpenIDSchema().from_dict(r2)
        standard_claims.verify()
        # add to the top level of the dict.
        for key, value in standard_claims.to_dict().items():
            result[key] = value

        return result
