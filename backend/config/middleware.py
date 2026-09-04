from django.middleware.security import SecurityMiddleware as DjangoSecurityMiddleware


class SecurityMiddleware(DjangoSecurityMiddleware):
    """HTTPS redirect with an exemption for in-container health probes.

    Docker/orchestrator healthchecks hit the app over plain HTTP even when
    SECURE_SSL_REDIRECT is on, so the health endpoint must stay reachable.
    """

    _HEALTH_PATHS = frozenset({"/api/health", "/api/health/"})

    def process_request(self, request):
        if request.path in self._HEALTH_PATHS:
            return None
        return super().process_request(request)
