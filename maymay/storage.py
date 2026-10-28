import hashlib
import mimetypes
from pathlib import Path
import boto3
from botocore.config import Config
from itsdangerous import URLSafeTimedSerializer

class Storage:
    def __init__(self,settings):
        self.s=settings
        self.signer=URLSafeTimedSerializer(settings.secret,salt='objects')
        self.client=boto3.client('s3',endpoint_url=settings.s3_endpoint or None,region_name=settings.s3_region,
            config=Config(signature_version='s3v4',s3={'addressing_style':'path'})) if settings.bucket else None
    def path(self,key):
        p=(self.s.data_dir/key).resolve()
        if not p.is_relative_to(self.s.data_dir) or key.startswith('/'):
            raise ValueError('Unsafe object path')
        return p
    def put(self,path,key):
        self.path(key) # same validation for both backends
        if self.client:
            self.client.upload_file(str(path),self.s.bucket,key,ExtraArgs={'ContentType':mimetypes.guess_type(key)[0] or 'application/octet-stream'})
        else:
            import shutil
            target=self.path(key);target.parent.mkdir(parents=True,exist_ok=True)
            if Path(path).resolve()!=target: shutil.copyfile(path,target)
    def get(self,key,path):
        self.path(key)
        if self.client:self.client.download_file(self.s.bucket,key,str(path))
        else:
            import shutil
            shutil.copyfile(self.path(key),path)
    def url(self,key,download=False):
        self.path(key)
        if self.client:
            params={'Bucket':self.s.bucket,'Key':key}
            if download:params['ResponseContentDisposition']='attachment'
            return self.client.generate_presigned_url('get_object',Params=params,ExpiresIn=3600)
        token=self.signer.dumps({'key':key,'download':download})
        return f'{self.s.public_url}/objects/{token}'
    def head(self,key):
        if self.client:return self.client.head_object(Bucket=self.s.bucket,Key=key)['ContentLength']
        return self.path(key).stat().st_size
    def remove(self,key):
        if self.client:self.client.delete_object(Bucket=self.s.bucket,Key=key)
        else:self.path(key).unlink(missing_ok=True)

def sha_file(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()
