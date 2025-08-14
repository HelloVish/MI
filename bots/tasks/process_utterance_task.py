import json
import logging
import time

import requests
from celery import shared_task

logger = logging.getLogger(__name__)

from bots.models import Credentials, RecordingManager, TranscriptionFailureReasons, TranscriptionProviders, Utterance
from bots.utils import pcm_to_mp3


def is_retryable_failure(failure_data):
    return failure_data.get("reason") in [
        TranscriptionFailureReasons.AUDIO_UPLOAD_FAILED,
        TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED,
        TranscriptionFailureReasons.TIMED_OUT,
        TranscriptionFailureReasons.RATE_LIMIT_EXCEEDED,
        TranscriptionFailureReasons.INTERNAL_ERROR,
    ]


def get_transcription(utterance, recording):
    try:
        if recording.transcription_provider == TranscriptionProviders.DEEPGRAM:
            transcription, failure_data = get_transcription_via_deepgram(utterance)
        elif recording.transcription_provider == TranscriptionProviders.GLADIA:
            transcription, failure_data = get_transcription_via_gladia(utterance)
        elif recording.transcription_provider == TranscriptionProviders.OPENAI:
            transcription, failure_data = get_transcription_via_openai(utterance)
        else:
            raise Exception(f"Unknown transcription provider: {recording.transcription_provider}")

        return transcription, failure_data
    except Exception as e:
        return None, {"reason": TranscriptionFailureReasons.INTERNAL_ERROR, "error": str(e)}


@shared_task(
    bind=True,
    soft_time_limit=3600,
    autoretry_for=(Exception,),
    retry_backoff=True,  # Enable exponential backoff
    max_retries=5,
)
def process_utterance(self, utterance_id):
    utterance = Utterance.objects.get(id=utterance_id)
    logger.info(f"Processing utterance {utterance_id}")

    recording = utterance.recording

    if utterance.failure_data:
        logger.info(f"process_utterance was called for utterance {utterance_id} but it has already failed, skipping")
        return

    if utterance.transcription is None:
        utterance.transcription_attempt_count += 1

        transcription, failure_data = get_transcription(utterance, recording)

        if failure_data:
            if utterance.transcription_attempt_count < 5 and is_retryable_failure(failure_data):
                utterance.save()
                raise Exception(f"Retryable failure when transcribing utterance {utterance_id}: {failure_data}")
            else:
                # Keep the audio blob around if it fails
                utterance.failure_data = failure_data
                utterance.save()
                logger.info(f"Transcription failed for utterance {utterance_id}, failure data: {failure_data}")
                return

        utterance.audio_blob = b""  # set the audio blob binary field to empty byte string
        utterance.transcription = transcription
        utterance.save()

        logger.info(f"Transcription complete for utterance {utterance_id}")

    # If the recording is in a terminal state and there are no more utterances to transcribe, set the recording's transcription state to complete
    if RecordingManager.is_terminal_state(utterance.recording.state) and Utterance.objects.filter(recording=utterance.recording, transcription__isnull=True).count() == 0:
        RecordingManager.set_recording_transcription_complete(utterance.recording)


def get_transcription_via_gladia(utterance):
    recording = utterance.recording
    gladia_credentials_record = recording.bot.project.credentials.filter(credential_type=Credentials.CredentialTypes.GLADIA).first()
    if not gladia_credentials_record:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND}

    gladia_credentials = gladia_credentials_record.get_credentials()
    if not gladia_credentials:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND}

    upload_url = "https://api.gladia.io/v2/upload"

    payload_mp3 = pcm_to_mp3(utterance.audio_blob.tobytes(), sample_rate=utterance.sample_rate)
    headers = {
        "x-gladia-key": gladia_credentials["api_key"],
    }
    files = {"audio": ("file.mp3", payload_mp3, "audio/mpeg")}
    upload_response = requests.request("POST", upload_url, headers=headers, files=files)

    if upload_response.status_code == 401:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_INVALID}

    if upload_response.status_code != 200 and upload_response.status_code != 201:
        return None, {"reason": TranscriptionFailureReasons.AUDIO_UPLOAD_FAILED, "status_code": upload_response.status_code}

    upload_response_json = upload_response.json()
    audio_url = upload_response_json["audio_url"]

    transcribe_url = "https://api.gladia.io/v2/pre-recorded"
    transcribe_request_body = {"audio_url": audio_url}
    if recording.bot.gladia_enable_code_switching():
        transcribe_request_body["enable_code_switching"] = True
        transcribe_request_body["code_switching_config"] = {
            "languages": recording.bot.gladia_code_switching_languages(),
        }
    transcribe_response = requests.request("POST", transcribe_url, headers=headers, json=transcribe_request_body)

    if transcribe_response.status_code != 200 and transcribe_response.status_code != 201:
        return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "step": "transcribe_request", "status_code": transcribe_response.status_code}

    transcribe_response_json = transcribe_response.json()
    result_url = transcribe_response_json["result_url"]

    # Poll the result_url until we get a completed transcription
    max_retries = 120  # Maximum number of retries (2 minutes with 1s sleep)
    retry_count = 0

    while retry_count < max_retries:
        result_response = requests.get(result_url, headers=headers)

        if result_response.status_code != 200:
            logger.error(f"Gladia result fetch failed with status code {result_response.status_code}")
            time.sleep(10)
            retry_count += 1
            continue

        result_data = result_response.json()
        status = result_data.get("status")

        if status == "done":
            # Transcription is complete
            transcription = result_data.get("result", {}).get("transcription", "")
            logger.info("Gladia transcription completed successfully, now deleting audio file from Gladia")
            # Delete the audio file from Gladia
            delete_response = requests.request("DELETE", result_url, headers=headers)
            if delete_response.status_code != 200 and delete_response.status_code != 202:
                logger.error(f"Gladia delete failed with status code {delete_response.status_code}")
            else:
                logger.info("Gladia delete successful")

            transcription["transcript"] = transcription["full_transcript"]
            del transcription["full_transcript"]

            # Extract all words from all utterances into a flat list
            all_words = []
            for utterance in transcription["utterances"]:
                if "words" in utterance:
                    all_words.extend(utterance["words"])
            transcription["words"] = all_words
            del transcription["utterances"]

            return transcription, None

        elif status == "error":
            error_code = result_data.get("error_code")
            return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "step": "transcribe_result_poll", "error_code": error_code}

        elif status in ["queued", "processing"]:
            # Still processing, wait and retry
            logger.info(f"Gladia transcription status: {status}, waiting...")
            time.sleep(1)
            retry_count += 1

        else:
            # Unknown status
            return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "step": "transcribe_result_poll", "status": status}

    # If we've reached here, we've timed out
    return None, {"reason": TranscriptionFailureReasons.TIMED_OUT, "step": "transcribe_result_poll"}


def get_transcription_via_deepgram(utterance):
    from deepgram import (
        DeepgramApiError,
        DeepgramClient,
        FileSource,
        PrerecordedOptions,
    )

    recording = utterance.recording
    payload: FileSource = {
        "buffer": utterance.audio_blob.tobytes(),
    }

    deepgram_model = recording.bot.deepgram_model()

    options = PrerecordedOptions(
        model=deepgram_model,
        smart_format=True,
        language=recording.bot.deepgram_language(),
        detect_language=recording.bot.deepgram_detect_language(),
        encoding="linear16",  # for 16-bit PCM
        sample_rate=utterance.sample_rate,
    )

    deepgram_credentials_record = recording.bot.project.credentials.filter(credential_type=Credentials.CredentialTypes.DEEPGRAM).first()
    if not deepgram_credentials_record:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND}

    deepgram_credentials = deepgram_credentials_record.get_credentials()
    if not deepgram_credentials:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND}

    deepgram = DeepgramClient(deepgram_credentials["api_key"])

    try:
        response = deepgram.listen.rest.v("1").transcribe_file(payload, options)
    except DeepgramApiError as e:
        original_error_json = json.loads(e.original_error)
        if original_error_json.get("err_code") == "INVALID_AUTH":
            return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_INVALID}
        return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "error_code": original_error_json.get("err_code")}

    logger.info(f"Deepgram transcription complete with model {deepgram_model}")
    return json.loads(response.results.channels[0].alternatives[0].to_json()), None


def get_transcription_via_openai(utterance):
    recording = utterance.recording
    openai_credentials_record = recording.bot.project.credentials.filter(credential_type=Credentials.CredentialTypes.OPENAI).first()
    if not openai_credentials_record:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND}

    openai_credentials = openai_credentials_record.get_credentials()
    if not openai_credentials:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_NOT_FOUND}

    # Convert PCM audio to MP3
    payload_mp3 = pcm_to_mp3(utterance.audio_blob.tobytes(), sample_rate=utterance.sample_rate)

    # Prepare the request for OpenAI's transcription API
    url = "https://api.openai.com/v1/audio/transcriptions"
    headers = {
        "Authorization": f"Bearer {openai_credentials['api_key']}",
    }
    files = {"file": ("file.mp3", payload_mp3, "audio/mpeg"), "model": (None, recording.bot.openai_transcription_model())}
    if recording.bot.openai_transcription_prompt():
        files["prompt"] = (None, recording.bot.openai_transcription_prompt())
    response = requests.post(url, headers=headers, files=files)

    if response.status_code == 401:
        return None, {"reason": TranscriptionFailureReasons.CREDENTIALS_INVALID}

    if response.status_code != 200:
        logger.error(f"OpenAI transcription failed with status code {response.status_code}: {response.text}")
        return None, {"reason": TranscriptionFailureReasons.TRANSCRIPTION_REQUEST_FAILED, "status_code": response.status_code}

    result = response.json()
    logger.info("OpenAI transcription completed successfully")

    # Format the response to match our expected schema
    transcription = {"transcript": result.get("text", "")}

    return transcription, None
