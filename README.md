# 🚀 MeetInsights Django Backend Setup

This project sets up the backend for MeetInsights, including user, organization, API key generation, and webhook subscriptions.

---
# GKE -- Push to GCR
```bash
docker buildx build --platform linux/amd64 -t gcr.io/clean-sunspot-456920-t5/mi:abc123def --push .
```

# For Local Setup
```bash
docker compose -f dev.docker-compose.yaml build
docker compose -f dev.docker-compose.yaml up
docker compose -f dev.docker-compose.yaml exec mi-app-local python manage.py migrate
```
# Guideline: 🚀 MeetInsights Django Backend Setup
```bash

python manage.py makemigrations accounts
python manage.py makemigrations bots
python manage.py migrate
```

# Django Shell
```bash

python manage.py shell



from accounts.models import *
from bots.models import *
# 1. Create default organization
default_org = Organization.objects.create(name="poc@meetinsights.in's organization")

# 2. Create a project under the organization
project = Project.objects.create(name="MI Project", organization=default_org)

# 3. Create a superuser
user = User.objects.create_superuser(
    email="poc@meetinsights.in",
    password="12345678",
    username="poc@meetinsights.in",
    organization=default_org
)

# 4. Generate and store an API Key
api_key = get_random_string(length=32)
key_hash = hashlib.sha256(api_key.encode()).hexdigest()

api_instance = ApiKey.objects.create(
    project=project,
    name='cars24',
    key_hash=key_hash
)

print("✅ Store this API key securely:", api_key)



# 5. Set up webhook subscription on Insight Server
WebhookSubscription.objects.create(
    project=project,
    url="https://example.com/webhook-endpoint"
)
```
