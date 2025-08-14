import logging

from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils.dateparse import parse_datetime
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
)
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .authentication import ApiKeyAuthentication
from .bots_api_utils import BotCreationSource, create_bot, create_bot_media_request_for_image, launch_bot, send_sync_command
from .models import (
    Bot,
    BotEventManager,
    BotEventSubTypes,
    BotEventTypes,
    BotMediaRequest,
    BotMediaRequestMediaTypes,
    BotMediaRequestStates,
    BotStates,
    Credentials,
    MediaBlob,
    MeetingTypes,
    Recording,
    Utterance,
)
from .serializers import (
    BotImageSerializer,
    BotSerializer,
    CreateBotSerializer,
    RecordingSerializer,
    SpeechSerializer,
    TranscriptUtteranceSerializer,
)
from .utils import meeting_type_from_url

TokenHeaderParameter = [
    OpenApiParameter(
        name="Authorization",
        type=str,
        location=OpenApiParameter.HEADER,
        description="API key for authentication",
        required=True,
        default="Token YOUR_API_KEY_HERE",
    ),
    OpenApiParameter(
        name="Content-Type",
        type=str,
        location=OpenApiParameter.HEADER,
        description="Should always be application/json",
        required=True,
        default="application/json",
    ),
]

LeavingBotExample = OpenApiExample(
    "Leaving Bot",
    value={
        "id": "bot_weIAju4OXNZkDTpZ",
        "meeting_url": "https://zoom.us/j/123?pwd=456",
        "state": "leaving",
        "events": [
            {"type": "join_requested", "created_at": "2024-01-18T12:34:56Z"},
            {"type": "joined_meeting", "created_at": "2024-01-18T12:35:00Z"},
            {"type": "leave_requested", "created_at": "2024-01-18T13:34:56Z"},
        ],
        "transcription_state": "in_progress",
        "recording_state": "in_progress",
    },
    description="Example response when requesting a bot to leave",
)

NewlyCreatedBotExample = OpenApiExample(
    "New bot",
    value={
        "id": "bot_weIAju4OXNZkDTpZ",
        "meeting_url": "https://zoom.us/j/123?pwd=456",
        "state": "joining",
        "events": [{"type": "join_requested", "created_at": "2024-01-18T12:34:56Z"}],
        "transcription_state": "not_started",
        "recording_state": "not_started",
    },
    description="Example response when creating a new bot",
)


@extend_schema(exclude=True)
class NotFoundView(APIView):
    def get(self, request, *args, **kwargs):
        return self.handle_request(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        return self.handle_request(request, *args, **kwargs)

    def put(self, request, *args, **kwargs):
        return self.handle_request(request, *args, **kwargs)

    def patch(self, request, *args, **kwargs):
        return self.handle_request(request, *args, **kwargs)

    def delete(self, request, *args, **kwargs):
        return self.handle_request(request, *args, **kwargs)

    def handle_request(self, request, *args, **kwargs):
        return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)


class BotCreateView(APIView):
    authentication_classes = [ApiKeyAuthentication]

    @extend_schema(
        operation_id="Create Bot",
        summary="Create a new bot",
        description="After being created, the bot will attempt to join the specified meeting.",
        request=CreateBotSerializer,
        responses={
            201: OpenApiResponse(
                response=BotSerializer,
                description="Bot created successfully",
                examples=[NewlyCreatedBotExample],
            ),
            400: OpenApiResponse(description="Invalid input"),
        },
        parameters=TokenHeaderParameter,
        tags=["Bots"],
    )
    def post(self, request):
        bot, error = create_bot(data=request.data, source=BotCreationSource.API, project=request.auth.project)
        if error:
            return Response(error, status=status.HTTP_400_BAD_REQUEST)

        launch_bot(bot)

        return Response(BotSerializer(bot).data, status=status.HTTP_201_CREATED)





class BotLeaveView(APIView):
    authentication_classes = [ApiKeyAuthentication]

    @extend_schema(
        operation_id="Leave Meeting",
        summary="Leave a meeting",
        description="Causes the bot to leave the meeting.",
        responses={
            200: OpenApiResponse(
                response=BotSerializer,
                description="Successfully requested to leave meeting",
                examples=[LeavingBotExample],
            ),
            400: OpenApiResponse(description="Bot is not in a valid state to leave the meeting"),
            404: OpenApiResponse(description="Bot not found"),
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Bot ID",
                examples=[OpenApiExample("Bot ID Example", value="bot_xxxxxxxxxxx")],
            ),
        ],
        tags=["Bots"],
    )
    def post(self, request, object_id):
        try:
            bot = Bot.objects.get(object_id=object_id, project=request.auth.project)

            BotEventManager.create_event(bot, BotEventTypes.LEAVE_REQUESTED, event_sub_type=BotEventSubTypes.LEAVE_REQUESTED_USER_REQUESTED)

            send_sync_command(bot)

            return Response(BotSerializer(bot).data, status=status.HTTP_200_OK)
        except ValidationError as e:
            logging.error(f"Error leaving meeting: {str(e)} (bot_id={object_id})")
            return Response({"error": e.messages[0]}, status=status.HTTP_400_BAD_REQUEST)
        except Bot.DoesNotExist:
            return Response({"error": "Bot not found"}, status=status.HTTP_404_NOT_FOUND)


class RecordingView(APIView):
    authentication_classes = [ApiKeyAuthentication]

    @extend_schema(
        operation_id="Get Bot Recording",
        summary="Get the recording for a bot",
        description="Returns a short-lived S3 URL for the recording of the bot.",
        responses={
            200: OpenApiResponse(
                response=RecordingSerializer,
                description="Short-lived S3 URL for the recording",
            )
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Bot ID",
                examples=[OpenApiExample("Bot ID Example", value="bot_xxxxxxxxxxx")],
            ),
        ],
        tags=["Bots"],
    )
    def get(self, request, object_id):
        try:
            bot = Bot.objects.get(object_id=object_id, project=request.auth.project)

            recording = Recording.objects.filter(bot=bot, is_default_recording=True).first()
            if not recording:
                return Response(
                    {"error": "No recording found for bot"},
                    status=status.HTTP_404_NOT_FOUND,
                )

            recording_file = recording.file
            if not recording_file:
                return Response(
                    {"error": "No recording file found for bot"},
                    status=status.HTTP_404_NOT_FOUND,
                )

            return Response(RecordingSerializer(recording).data)

        except Bot.DoesNotExist:
            return Response({"error": "Bot not found"}, status=status.HTTP_404_NOT_FOUND)


class TranscriptView(APIView):
    authentication_classes = [ApiKeyAuthentication]

    @extend_schema(
        operation_id="Get Bot Transcript",
        summary="Get the transcript for a bot",
        description="If the meeting is still in progress, this returns the transcript so far.",
        responses={
            200: OpenApiResponse(
                response=TranscriptUtteranceSerializer(many=True),
                description="List of transcribed utterances",
            ),
            404: OpenApiResponse(description="Bot not found"),
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Bot ID",
                examples=[OpenApiExample("Bot ID Example", value="bot_xxxxxxxxxxx")],
            ),
            OpenApiParameter(
                name="updated_after",
                type={"type": "string", "format": "ISO 8601 datetime"},
                location=OpenApiParameter.QUERY,
                description="Only return transcript entries updated or created after this time. Useful when polling for updates to the transcript.",
                required=False,
                examples=[OpenApiExample("DateTime Example", value="2024-01-18T12:34:56Z")],
            ),
        ],
        tags=["Bots"],
    )
    def get(self, request, object_id):
        try:
            bot = Bot.objects.get(object_id=object_id, project=request.auth.project)

            recording = Recording.objects.filter(bot=bot, is_default_recording=True).first()
            if not recording:
                return Response(
                    {"error": "No recording found for bot"},
                    status=status.HTTP_404_NOT_FOUND,
                )

            # Get all utterances with transcriptions, sorted by timeline
            utterances_query = Utterance.objects.select_related("participant").filter(recording=recording, transcription__isnull=False)

            # Apply updated_after filter if provided
            updated_after = request.query_params.get("updated_after")
            if updated_after:
                try:
                    updated_after_datetime = parse_datetime(str(updated_after))
                except Exception:
                    updated_after_datetime = None

                if not updated_after_datetime:
                    return Response(
                        {"error": "Invalid updated_after format. Use ISO 8601 format (e.g., 2024-01-18T12:34:56Z)"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                utterances_query = utterances_query.filter(updated_at__gt=updated_after_datetime)

            # Apply ordering
            utterances = utterances_query.order_by("timestamp_ms")

            # Format the response, skipping empty transcriptions
            transcript_data = [
                {
                    "speaker_name": utterance.participant.full_name,
                    "speaker_uuid": utterance.participant.uuid,
                    "speaker_user_uuid": utterance.participant.user_uuid,
                    "timestamp_ms": utterance.timestamp_ms,
                    "duration_ms": utterance.duration_ms,
                    "transcription": utterance.transcription,
                }
                for utterance in utterances
                if utterance.transcription.get("transcript", "")
            ]

            serializer = TranscriptUtteranceSerializer(transcript_data, many=True)
            return Response(serializer.data)

        except Bot.DoesNotExist:
            return Response({"error": "Bot not found"}, status=status.HTTP_404_NOT_FOUND)


class BotDetailView(APIView):
    authentication_classes = [ApiKeyAuthentication]

    @extend_schema(
        operation_id="Get Bot",
        summary="Get the details for a bot",
        responses={
            200: OpenApiResponse(
                response=BotSerializer,
                description="Bot details",
                examples=[NewlyCreatedBotExample],
            ),
            404: OpenApiResponse(description="Bot not found"),
        },
        parameters=[
            *TokenHeaderParameter,
            OpenApiParameter(
                name="object_id",
                type=str,
                location=OpenApiParameter.PATH,
                description="Bot ID",
                examples=[OpenApiExample("Bot ID Example", value="bot_xxxxxxxxxxx")],
            ),
        ],
        tags=["Bots"],
    )
    def get(self, request, object_id):
        try:
            bot = Bot.objects.get(object_id=object_id, project=request.auth.project)
            return Response(BotSerializer(bot).data)

        except Bot.DoesNotExist:
            return Response({"error": "Bot not found"}, status=status.HTTP_404_NOT_FOUND)
