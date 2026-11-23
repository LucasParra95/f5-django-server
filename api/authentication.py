from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken
from rest_framework.exceptions import AuthenticationFailed
from api.models import Usuario


class CustomJWTAuthentication(JWTAuthentication):
    """
    JWT sobre el modelo Usuario propio.

    Las vistas con `ignorar_token_invalido = True` (invitaciones públicas) tratan un
    token vencido o inválido como anónimo en vez de responder 401. El resto de las
    vistas siguen respondiendo 401, que es lo que el frontend usa para refrescar.
    """

    def authenticate(self, request):
        header = self.get_header(request)
        if header is None:
            return None

        raw_token = self.get_raw_token(header)
        if raw_token is None:
            return None

        try:
            validated_token = self.get_validated_token(raw_token)
        except InvalidToken:
            contexto = getattr(request, 'parser_context', None) or {}
            if getattr(contexto.get('view'), 'ignorar_token_invalido', False):
                return None
            raise

        user_id = validated_token.get('user_id')
        if not user_id:
            raise AuthenticationFailed('Token no contiene user_id')

        try:
            user = Usuario.objects.get(id=user_id)
        except Usuario.DoesNotExist:
            raise AuthenticationFailed('Usuario no encontrado')

        if not user.is_active:
            raise AuthenticationFailed('Usuario inactivo')

        return (user, validated_token)