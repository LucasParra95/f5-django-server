from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework.exceptions import AuthenticationFailed
from api.models import Usuario

class CustomJWTAuthentication(JWTAuthentication):
    """Autenticación JWT personalizada que busca en modelo Usuario custom"""
    
    def authenticate(self, request):
        header = self.get_header(request)
        
        if header is None:
            return None

        raw_token = self.get_raw_token(header)
        if raw_token is None:
            return None

        validated_token = self.get_validated_token(raw_token)
        
        # Obtener user_id del token
        user_id = validated_token.get('user_id')
        
        if not user_id:
            raise AuthenticationFailed('Token no contiene user_id')
        
        try:
            user = Usuario.objects.get(id=user_id)
        except Usuario.DoesNotExist:
            raise AuthenticationFailed('Usuario no encontrado')
        
        return (user, validated_token)