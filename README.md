# garmin-mcp

Serveur MCP personnel, en lecture seule, devant l'API Garmin Connect Personal.
La clé Garmin reste sur la VM ; Claude ne voit qu'un jeton d'accès au serveur.

## Déploiement (Docker + Traefik)

```bash
cp .env.example .env && chmod 600 .env
# Remplir .env : clé Garmin, MCP_AUTH_TOKEN (openssl rand -hex 32), domaine, réglages Traefik
docker compose up -d --build
curl https://<MCP_DOMAIN>/healthz          # {"status":"ok"}
curl -o /dev/null -w '%{http_code}\n' -X POST https://<MCP_DOMAIN>/mcp   # 401 attendu
```

Le DNS de `MCP_DOMAIN` doit pointer sur la VM, et le serveur doit être joignable
depuis Internet (Claude s'y connecte depuis le cloud d'Anthropic).

## Connexion dans Claude

Connecteurs > Ajouter un connecteur personnalisé :
- URL : `https://<MCP_DOMAIN>/mcp`
- Authentification : pas de connexion (pas d'OAuth)
- En-tête de requête : `Authorization` = `Bearer <MCP_AUTH_TOKEN>`

## Outils

`garmin_weight`, `garmin_activities`, `garmin_activity`, `garmin_sleep`,
`garmin_daily_summaries`, `garmin_hrv`, `garmin_body_battery`, et `garmin_get`
(GET libre sur n'importe quel chemin de l'API).

## À savoir

- La clé Garmin expire (30 jours à 1 an selon ton choix) : la régénérer puis `docker compose up -d`.
- Le jeton d'accès Garmin (24 h) est renouvelé automatiquement, en mémoire.
- `garmin_sleep` et `garmin_daily_summaries` renvoient une ligne courte par nuit ou par jour ; `detail=true` donne la réponse Garmin complète.
- Les longues listes sont tronquées à `MAX_LIST_ITEMS` (40) ; `garmin_get` accepte `full=true`.
- Chemins d'API repris du client officiel github.com/garmin/connect-personal-api-python.
