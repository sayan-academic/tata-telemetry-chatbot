# 1. Base OS & Python Runtime
FROM python:3.12.3-slim

# 2. Environment Variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# 3. Working Directory
WORKDIR /app

# 4. Dependency Installation
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# 5. Copy Source Code
COPY . /app/

# 6. Collect Static Files (NEW)
# We inject a fake secret key just to bypass Django's startup checks during compilation
RUN SECRET_KEY="build-dummy-key" python manage.py collectstatic --no-input

# 7. Expose Port
EXPOSE 8000

# 8. Execution Command (Dynamic Port Binding)
CMD ["sh", "-c", "gunicorn tata_chatbot.wsgi:application --bind 0.0.0.0:${PORT:-8000} --timeout 120"]