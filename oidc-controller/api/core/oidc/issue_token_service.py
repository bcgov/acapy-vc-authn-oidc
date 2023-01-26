import logging
import json
import uuid
import dataclasses
from datetime import datetime
from typing import List, Dict, Any
from pydantic import BaseModel

from ...authSessions.models import AuthSession
from ...verificationConfigs.models import VerificationConfig

logger = logging.getLogger(__name__)


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
        cls, pres_exch: Dict, auth_session: AuthSession, ver_config: VerificationConfig
    ) -> List["Claim"]:
        """Converts vc presentation values to oidc claims"""
        logger.debug(f">>> Token.get_claims")
        logger.info(pres_exch)

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

        for referent, requested_attr in auth_session.presentation_exchange[
            "presentation_request"
        ]["requested_attributes"].items():
            # loop through each value and put it in token as a claim
            revealed_attrs: Dict[str, Any] = auth_session.presentation_exchange[
                "presentation"
            ]["requested_proof"]["revealed_attrs"]
            presentation_claims[requested_attr["name"]] = Claim(
                type=requested_attr["name"], value=revealed_attrs[referent]["raw"]
            )

        # look at all presentation_claims and one should match the configured subject_identifier
        sub_id_value = None
        sub_id_claim = presentation_claims.get(ver_config.subject_identifier)

        if not sub_id_claim:
            logger.warning(
                "subject_identifer not found in presentation values, generating random subject_identifier"
            )
            sub_id_value = uuid.uuid4()
        else:
            sub_id_value = sub_id_claim.value

        # add sub and append presentation_claims
        oidc_claims.append(Claim(type="sub", value=sub_id_value))
        presentation_claims.values()

        result = {c.type: c.value for c in oidc_claims}
        result["vc_proof_claims"] = json.dumps(
            {c.type: c.value for c in presentation_claims.values()}
        )
        return result

    # renames and calculates dict members appropriate to https://openid.net/specs/openid-connect-core-1_0.html#IDToken
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
        logger.info(f"<<< idtoken_dict r={result}")

        return result
