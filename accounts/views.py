from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
import json
import logging

logger = logging.getLogger(__name__)

def home(request):
    return JsonResponse({'message': 'This is home page!'})

@csrf_exempt  # For testing purposes; remove or secure in production
def webhook_tests(request):
    if request.method == "POST":
        try:
            payload = json.loads(request.body.decode("utf-8"))
            print('Received webhook payload:')
            print(payload)
            logger.info(f"Received webhook payload: {payload}")
            # You can process the payload here

            return JsonResponse({"status": "success", "data": payload}, status=200)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)
    else:
        return JsonResponse({"error": "Only POST method allowed"}, status=405)
