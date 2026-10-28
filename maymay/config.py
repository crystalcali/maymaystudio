import os
from dataclasses import dataclass
from pathlib import Path

@dataclass
class Settings:
    production: bool = os.getenv('STUDIO_ENV', 'development') == 'production'
    database_url: str = os.getenv('DATABASE_URL', 'sqlite:///./studio.db')
    public_url: str = os.getenv('PUBLIC_URL', 'http://localhost:8000').rstrip('/')
    secret: str = os.getenv('SECRET_KEY', 'local-development-change-this-secret-12345')
    password: str = os.getenv('ADMIN_PASSWORD', 'maymay-local')
    data_dir: Path = Path(os.getenv('DATA_DIR', './data')).resolve()
    bucket: str = os.getenv('S3_BUCKET', '')
    s3_endpoint: str = os.getenv('S3_ENDPOINT_URL', '')
    s3_region: str = os.getenv('S3_REGION', 'ru-1')
    agent_image: str = os.getenv('AGENT_IMAGE', '')
    lease_seconds: int = 120
    max_upload: int = int(os.getenv('MAX_UPLOAD_MB', '1024')) * 1024 * 1024
    def validate(self):
        if self.database_url.startswith('postgres://'):
            self.database_url = self.database_url.replace('postgres://','postgresql+psycopg://',1)
        if self.database_url.startswith('postgresql://'):
            self.database_url = self.database_url.replace('postgresql://','postgresql+psycopg://',1)
        if self.production:
            if len(self.secret)<32 or self.secret.startswith('local-development'):
                raise RuntimeError('Set a random persistent SECRET_KEY (32+ characters)')
            if len(self.password)<14 or self.password=='maymay-local':
                raise RuntimeError('Set ADMIN_PASSWORD (14+ characters)')
            if not os.getenv('AWS_ACCESS_KEY_ID') or not os.getenv('AWS_SECRET_ACCESS_KEY'):
                raise RuntimeError('Production requires explicit S3 access credentials')
            if not self.s3_endpoint.startswith('https://'):
                raise RuntimeError('S3_ENDPOINT_URL must use HTTPS')
            if not self.public_url.startswith('https://') or not self.bucket:
                raise RuntimeError('Production requires PUBLIC_URL=https://… and S3_BUCKET')
            if not self.database_url.startswith('postgresql+psycopg://'):
                raise RuntimeError('Production requires external PostgreSQL')
        self.data_dir.mkdir(parents=True,exist_ok=True)
        return self
