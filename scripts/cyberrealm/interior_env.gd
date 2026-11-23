extends Node
## Porte l'Environment d'un intérieur (remplace son WorldEnvironment d'origine).
## Un export est sérialisé tel quel par le baker LAN ; interiors.gd le pose sur
## la caméra du joueur local à l'entrée (Camera3D.environment), sans toucher à
## l'environnement du niveau.

@export var environment: Environment
