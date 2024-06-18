import base64
import io
import json
import uuid
from datetime import datetime
from typing import cast
from urllib.parse import urlencode

import qrcode
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import status as http_status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jinja2 import Template
from oic.oic.message import AuthorizationRequest
from pymongo.database import Database
from pyop.exceptions import InvalidAuthenticationRequest

from ..authSessions.crud import AuthSessionCreate, AuthSessionCRUD
from ..authSessions.models import AuthSessionPatch, AuthSessionState
from ..core.acapy.client import AcapyClient
from ..core.aries import (
    OOBServiceDecorator,
    OutOfBandMessage,
    OutOfBandPresentProofAttachment,
    PresentationRequestMessage,
    PresentProofv10Attachment,
    ServiceDecorator,
)
from ..core.config import settings
from ..core.logger_util import log_debug
from ..core.oidc import provider
from ..core.oidc.issue_token_service import Token
from ..db.session import get_db

# Access to the websocket
from ..routers.socketio import connections_reload, sio

# This allows the templates to insert assets like css, js or svg.
from ..templates.helpers import add_asset
from ..verificationConfigs.crud import VerificationConfigCRUD

ChallengePollUri = "/poll"
AuthorizeCallbackUri = "/callback"
VerifiedCredentialAuthorizeUri = f"/{provider.AuthorizeUriEndpoint}"
VerifiedCredentialTokenUri = f"/{provider.TokenUriEndpoint}"

logger: structlog.typing.FilteringBoundLogger = structlog.getLogger(__name__)

router = APIRouter()


@log_debug
# TODO: To be replaced by a websocket and a python scheduler
# TODO: This is a hack to get the websocket to expire the proof, if necessary
@router.get(f"{ChallengePollUri}/{{pid}}")
async def poll_pres_exch_complete(pid: str, db: Database = Depends(get_db)):
    """Called by authorize webpage to see if request
    is verified and token issuance can proceed."""
    auth_session = await AuthSessionCRUD(db).get(pid)

    pid = str(auth_session.id)
    connections = connections_reload()
    sid = connections.get(pid)

    """
     Check if proof is expired. But only if the proof has not been started.
     NOTE: This should eventually be moved to a background task.
    """
    if (
        auth_session.expired_timestamp < datetime.now()
        and auth_session.proof_status == AuthSessionState.NOT_STARTED
    ):
        logger.info("PROOF EXPIRED")
        auth_session.proof_status = AuthSessionState.EXPIRED
        await AuthSessionCRUD(db).patch(
            str(auth_session.id), AuthSessionPatch(**auth_session.dict())
        )
        # Send message through the websocket.
        await sio.emit("status", {"status": "expired"}, to=sid)

    return {"proof_status": auth_session.proof_status}


@log_debug
@router.get(VerifiedCredentialAuthorizeUri, response_class=HTMLResponse)
async def get_authorize(request: Request, db: Database = Depends(get_db)):
    """Called by oidc platform."""
    logger.debug(">>> get_authorize")

    # Verify OIDC forward payload
    model = AuthorizationRequest().from_dict(request.query_params._dict)
    model.verify()

    try:
        auth_req = provider.provider.parse_authentication_request(
            urlencode(request.query_params._dict), request.headers
        )
    except InvalidAuthenticationRequest as e:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid auth request: {e}",
        )

    #  create proof for this request
    new_user_id = str(uuid.uuid4())
    authn_response = provider.provider.authorize(model, new_user_id)

    # retrieve presentation_request config.
    client = AcapyClient()
    ver_config_id = model.get("pres_req_conf_id")
    ver_config = await VerificationConfigCRUD(db).get(ver_config_id)

    # Create presentation_request to show on screen
    response = client.create_presentation_request(ver_config.generate_proof_request())
    pres_exch_dict = response.dict()

    # Prepeare the presentation request
    client = AcapyClient()
    use_public_did = not settings.USE_OOB_LOCAL_DID_SERVICE
    wallet_did = client.get_wallet_did(public=use_public_did)

    byo_attachment = PresentProofv10Attachment.build(pres_exch_dict["pres_request"])

    msg = None
    if settings.USE_OOB_PRESENT_PROOF:
        if settings.USE_OOB_LOCAL_DID_SERVICE:
            oob_s_d = OOBServiceDecorator(
                service_endpoint=client.service_endpoint,
                recipient_keys=[wallet_did.verkey],
            ).dict()
        else:
            oob_s_d = wallet_did.verkey

        msg = PresentationRequestMessage(
            id=pres_exch_dict["thread_id"],
            request=[byo_attachment],
        )
        oob_msg = OutOfBandMessage(
            request_attachments=[
                OutOfBandPresentProofAttachment(
                    id="request-0",
                    data={"json": msg.dict(by_alias=True)},
                )
            ],
            id=pres_exch_dict["thread_id"],
            services=[oob_s_d],
        )
        msg_contents = oob_msg
    else:
        s_d = ServiceDecorator(
            service_endpoint=client.service_endpoint, recipient_keys=[wallet_did.verkey]
        )
        msg = PresentationRequestMessage(
            id=pres_exch_dict["thread_id"],
            request=[byo_attachment],
            service=s_d,
        )
        msg_contents = msg

    # Create and save OIDC AuthSession
    new_auth_session = AuthSessionCreate(
        response_url=authn_response.request(auth_req["redirect_uri"]),
        pyop_auth_code=authn_response["code"],
        request_parameters=model.to_dict(),
        ver_config_id=ver_config_id,
        pres_exch_id=response.pres_ex_id,
        presentation_exchange=pres_exch_dict,
        presentation_request_msg=msg_contents.dict(by_alias=True),
    )
    auth_session = await AuthSessionCRUD(db).create(new_auth_session)

    formated_msg = json.dumps(msg_contents.dict(by_alias=True))

    # QR CONTENTS
    controller_host = settings.CONTROLLER_URL
    url_to_message = (
        controller_host + "/url/pres_exch/" + str(auth_session.pres_exch_id)
    )
    # CREATE the image
    buff = io.BytesIO()
    qrcode.make(url_to_message).save(buff, format="PNG")
    image_contents = base64.b64encode(buff.getvalue()).decode("utf-8")
    callback_url = f"""{controller_host}{AuthorizeCallbackUri}?pid={auth_session.id}"""

    # BC Wallet deep link
    # base64 encode the formated_msg
    base64_msg = base64.b64encode(formated_msg.encode("utf-8")).decode("utf-8")
    wallet_deep_link = f"bcwallet://aries_proof-request?c_i={base64_msg}"

    # This is the payload to send to the template
    data = {
        "image_contents": image_contents,
        "url_to_message": url_to_message,
        "callback_url": callback_url,
        "add_asset": add_asset,
        "pres_exch_id": auth_session.pres_exch_id,
        "pid": auth_session.id,
        "controller_host": controller_host,
        "challenge_poll_uri": ChallengePollUri,
        "wallet_deep_link": wallet_deep_link,
    }

    # Prepare the template
    template_file = open("api/templates/verified_credentials.html", "r").read()
    template = Template(template_file)

    # Render and return the template
    return template.render(data)


@log_debug
@router.get("/callback", response_class=RedirectResponse)
async def get_authorize_callback(pid: str, db: Database = Depends(get_db)):
    """Called by Authorize page when verification is complete"""
    auth_session = await AuthSessionCRUD(db).get(pid)

    url = auth_session.response_url
    return RedirectResponse(url)


@log_debug
@router.post(VerifiedCredentialTokenUri, response_class=JSONResponse)
async def post_token(request: Request, db: Database = Depends(get_db)):
    """Called by oidc platform to retrieve token contents"""
    async with request.form() as form:
        logger.warn(f"post_token: form was {form}")
        form_dict = cast(dict[str, str], form._dict)
        auth_session = await AuthSessionCRUD(db).get_by_pyop_auth_code(
            form_dict["code"]
        )
        ver_config = await VerificationConfigCRUD(db).get(auth_session.ver_config_id)
        claims = Token.get_claims(auth_session, ver_config)

        # Replace auto-generated sub with one coming from proof, if available
        # The stateless storage uses a cypher, so a new item can be added and
        # the reference in the form needs to be updated with the new key value
        if claims.get("sub"):
            authz_info = provider.provider.authz_state.authorization_codes[
                form_dict["code"]
            ]
            authz_info["sub"] = claims.pop("sub")
            new_code = provider.provider.authz_state.authorization_codes.pack(
                authz_info
            )
            form_dict["code"] = new_code

        # convert form data to what library expects, Flask.app.request.get_data()
        data = urlencode(form_dict)
        token_response = provider.provider.handle_token_request(
            data, request.headers, claims
        )
        logger.debug(f"Token response: {token_response.to_dict()}")
        return token_response.to_dict()
