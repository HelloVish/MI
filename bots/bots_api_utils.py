import json
import logging
import os
from enum import Enum

import redis
from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse

from .models import (
    Bot,
    BotEventManager,
    BotEventTypes,
    BotMediaRequest,
    BotMediaRequestMediaTypes,
    Credentials,
    MediaBlob,
    MeetingTypes,
    Project,
    Recording,
    RecordingTypes,
    TranscriptionTypes,
)
from .serializers import (
    CreateBotSerializer,
)
from .tasks import run_bot
from .utils import meeting_type_from_url, transcription_provider_from_meeting_url_and_transcription_settings

logger = logging.getLogger(__name__)


def send_sync_command(bot, command="sync"):
    redis_url = os.getenv("REDIS_URL")
    redis_client = redis.from_url(redis_url)
    channel = f"bot_{bot.id}"
    message = {"command": command}
    redis_client.publish(channel, json.dumps(message))


def launch_bot(bot):
    # If this instance is running in Kubernetes, use the Kubernetes pod creator
    # which spins up a new pod for the bot
    if os.getenv("LAUNCH_BOT_METHOD") == "kubernetes":
        from .bot_pod_creator import BotPodCreator

        bot_pod_creator = BotPodCreator()
        create_pod_result = bot_pod_creator.create_bot_pod(bot_id=bot.id, bot_name=bot.k8s_pod_name())
        logger.info(f"Bot {bot.id} launched via Kubernetes: {create_pod_result}")
    else:
        # Default to launching bot via celery
        run_bot.delay(bot.id)


def create_bot_media_request_for_image(bot, image):
    content_type = image["type"]
    image_data = image["decoded_data"]
    try:
        # Create or get existing MediaBlob
        media_blob = MediaBlob.get_or_create_from_blob(project=bot.project, blob=image_data, content_type=content_type)
    except Exception as e:
        error_message_first_line = str(e).split("\n")[0]
        logging.error(f"Error creating image blob: {error_message_first_line} (content_type={content_type})")
        raise ValidationError(f"Error creating the image blob: {error_message_first_line}.")

    # Create BotMediaRequest
    BotMediaRequest.objects.create(
        bot=bot,
        media_blob=media_blob,
        media_type=BotMediaRequestMediaTypes.IMAGE,
    )


def validate_meeting_url_and_credentials(meeting_url, project):
    """
    Validates meeting URL format and required credentials.
    Returns error message if validation fails, None if validation succeeds.
    """

    if meeting_type_from_url(meeting_url) == MeetingTypes.ZOOM:
        zoom_credentials = project.credentials.filter(credential_type=Credentials.CredentialTypes.ZOOM_OAUTH).first()
        if not zoom_credentials:
            return {"error": "Zoom App credentials are required to create a Zoom bot. Please contact admin for Zoom credentials."}

    return None


class BotCreationSource(str, Enum):
    API = "api"
    DASHBOARD = "dashboard"


def create_bot(data: dict, source: BotCreationSource, project: Project) -> tuple[Bot | None, dict | None]:
    # Given them a small grace period before we start rejecting requests
    if project.organization.credits() < -1:
        logger.error(f"Organization {project.organization.id} has insufficient credits. Please add credits in the Settings -> Billing page.")
        return None, {"error": "Organization has run out of credits. Please add more credits in the Settings -> Billing page."}

    serializer = CreateBotSerializer(data=data)
    if not serializer.is_valid():
        return None, serializer.errors

    # Access the bot through the api key
    meeting_url = serializer.validated_data["meeting_url"]

    error = validate_meeting_url_and_credentials(meeting_url, project)
    if error:
        return None, error

    bot_name = serializer.validated_data["bot_name"]
    transcription_settings = serializer.validated_data["transcription_settings"]
    rtmp_settings = serializer.validated_data["rtmp_settings"]
    recording_settings = serializer.validated_data["recording_settings"]
    debug_settings = serializer.validated_data["debug_settings"]
    automatic_leave_settings = serializer.validated_data["automatic_leave_settings"]
    bot_image = serializer.validated_data["bot_image"]
    metadata = serializer.validated_data["metadata"]

    settings = {
        "transcription_settings": transcription_settings,
        "rtmp_settings": rtmp_settings,
        "recording_settings": recording_settings,
        "debug_settings": debug_settings,
        "automatic_leave_settings": automatic_leave_settings,
    }

    with transaction.atomic():
        bot = Bot.objects.create(
            project=project,
            meeting_url=meeting_url,
            name=bot_name,
            settings=settings,
            metadata=metadata,
        )

        Recording.objects.create(
            bot=bot,
            recording_type=RecordingTypes.AUDIO_AND_VIDEO,
            transcription_type=TranscriptionTypes.NON_REALTIME,
            transcription_provider=transcription_provider_from_meeting_url_and_transcription_settings(meeting_url, transcription_settings),
            is_default_recording=True,
        )

        if bot_image:
            try:
                create_bot_media_request_for_image(bot, bot_image)
            except ValidationError as e:
                return None, {"error": e.messages[0]}

        # Try to transition the state from READY to JOINING
        BotEventManager.create_event(bot=bot, event_type=BotEventTypes.JOIN_REQUESTED, event_metadata={"source": source})

        return bot, None
