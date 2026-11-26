extends Node
## Cascades animées. Morrowind fait défiler la texture des cascades (tx_waterfall_*) vers le bas ; l'export
## glTF perd ce contrôleur d'UV, donc l'eau restait figée. On fait défiler le matériau à l'exécution.
##
## Les meshes dont le NOM contient « waterfall » coulent vite (V croît vers le bas du mesh). Les tronçons de
## rivière « terrwater » (canaux) partagent la même texture, mais leur V court le long du lit : ils reçoivent leur
## propre copie du matériau, animée plus lentement. Tout autre mesh qui partage la texture en reçoit une copie fixe.
## Le décalage dépend de l'horloge (pas d'un état accumulé) : toutes les machines voient la même eau, et rien
## n'est à sérialiser dans le blob LAN.

const SPEED := 1.4        # tours de texture par seconde (V croît vers le bas du mesh : décalage négatif = vers le bas)

const RIVER_SPEED := 1.4  # idem pour les rivières ; mettre un signe négatif pour inverser le courant

var _mats: Array[BaseMaterial3D] = []
var _river_mats: Array[BaseMaterial3D] = []

func _ready() -> void:
	var meshes: Array[MeshInstance3D] = []
	for mi in get_parent().find_children("*", "MeshInstance3D", true, false):
		if (mi as MeshInstance3D).mesh != null:
			meshes.append(mi)
	var seen := {}
	# Passe 1 : matériaux des meshes « waterfall » (ceux qui coulent).
	for mi in meshes:
		if String(mi.name).to_lower().contains("waterfall"):
			for m in _fall_mats(mi):
				if not seen.has(m):
					seen[m] = true
					_mats.append(m)
	# Passe 2 : les autres meshes qui partagent un de ces matériaux en reçoivent une copie (rivière : animée).
	var river_copy := {}      # matériau d'origine -> copie de rivière
	for mi in meshes:
		var nm := String(mi.name).to_lower()
		if nm.contains("waterfall"):
			continue
		var river := nm.contains("terrwater")
		var mesh := mi.mesh
		for s in mesh.get_surface_count():
			var m := mesh.surface_get_material(s) as BaseMaterial3D
			if m == null:
				continue
			if river:
				if not String(m.resource_name).to_lower().contains("waterfall"):
					continue
			elif not seen.has(m):
				continue
			if not river:
				mesh.surface_set_material(s, m.duplicate())
				continue
			if not river_copy.has(m):
				river_copy[m] = m.duplicate()
				_river_mats.append(river_copy[m])
			mesh.surface_set_material(s, river_copy[m])
	set_process(not _mats.is_empty() or not _river_mats.is_empty())

func _fall_mats(mi: MeshInstance3D) -> Array[BaseMaterial3D]:
	var out: Array[BaseMaterial3D] = []
	for s in mi.mesh.get_surface_count():
		var m := mi.mesh.surface_get_material(s) as BaseMaterial3D
		if m != null and String(m.resource_name).to_lower().contains("waterfall"):
			out.append(m)
	return out

func _process(_delta: float) -> void:
	var t := Time.get_ticks_msec() * 0.001
	var off := -fposmod(t * SPEED, 1.0)
	for m in _mats:
		m.uv1_offset.y = off
	off = -fposmod(t * RIVER_SPEED, 1.0)
	for m in _river_mats:
		m.uv1_offset.y = off
