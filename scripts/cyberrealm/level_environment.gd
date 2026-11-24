extends Node
## Les réglages du WorldEnvironment du niveau priment sur les réglages
## graphiques du menu pause (comme le documente wayland_room.gd) : un effet que
## le niveau active ne peut pas être coupé par le menu, et les ajustements de
## couleur (luminosité / contraste / saturation) sont ceux du menu ; ceux du niveau ne sont que le défaut.
## Les effets que le niveau n'active pas restent réglables dans le menu.
##
## Les valeurs de référence sont des exports de ce nœud (copiées de
## l'Environment du niveau par integrate.py) : elles voyagent dans le blob LAN
## et ne dépendent pas de l'état — déjà modifié par le menu — de l'Environment.

@export var ssr_enabled := false
@export var ssao_enabled := false
@export var ssil_enabled := false
@export var sdfgi_enabled := false
@export var glow_enabled := false
@export var fog_enabled := false
@export var volumetric_fog_enabled := false
@export var adjustment_enabled := false
@export var adjustment_brightness := 1.0
@export var adjustment_contrast := 1.0
@export var adjustment_saturation := 1.0

const TOGGLES := ["ssr_enabled", "ssao_enabled", "ssil_enabled", "sdfgi_enabled",
	"glow_enabled", "fog_enabled", "volumetric_fog_enabled"]
const ADJUST := ["adjustment_brightness", "adjustment_contrast", "adjustment_saturation"]

# Seuls les toggles sont imposés : luminosité / contraste / saturation restent
# pilotés par le menu pause (les fenêtres 3D les compensent via window_bcs).
# Vérifié à chaque frame : le menu applique ses réglages au démarrage, après un
# changement de niveau LAN et à chaque modification, et rien d'autre n'avertit.
func _process(_delta: float) -> void:
	var we := get_parent().get_node_or_null("WorldEnvironment") as WorldEnvironment
	if we == null or we.environment == null:
		return
	var env := we.environment
	for k in TOGGLES:
		if get(k) and not env.get(k):
			env.set(k, true)
