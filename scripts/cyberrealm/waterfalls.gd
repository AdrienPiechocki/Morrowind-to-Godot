extends Node
## Cascades animées. Morrowind fait défiler la texture des cascades (tx_waterfall_*) vers le bas ; l'export
## glTF perd ce contrôleur d'UV, donc l'eau restait figée. On fait défiler le matériau à l'exécution.
##
## Seuls les meshes dont le NOM contient « waterfall » coulent : d'autres meshes (tronçons de rivière) partagent
## la même texture mais n'ont pas la même orientation d'UV ; ils reçoivent une copie fixe du matériau.
## Le décalage dépend de l'horloge (pas d'un état accumulé) : toutes les machines voient la même eau, et rien
## n'est à sérialiser dans le blob LAN.

const SPEED := 0.7        # tours de texture par seconde (V croît vers le bas du mesh : décalage négatif = vers le bas)

var _mats: Array[BaseMaterial3D] = []

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
	# Passe 2 : les autres meshes qui partagent un de ces matériaux en reçoivent une copie fixe.
	for mi in meshes:
		if not String(mi.name).to_lower().contains("waterfall"):
			var mesh := mi.mesh
			for s in mesh.get_surface_count():
				var m := mesh.surface_get_material(s) as BaseMaterial3D
				if m != null and seen.has(m):
					mesh.surface_set_material(s, m.duplicate())
	set_process(not _mats.is_empty())

func _fall_mats(mi: MeshInstance3D) -> Array[BaseMaterial3D]:
	var out: Array[BaseMaterial3D] = []
	for s in mi.mesh.get_surface_count():
		var m := mi.mesh.surface_get_material(s) as BaseMaterial3D
		if m != null and String(m.resource_name).to_lower().contains("waterfall"):
			out.append(m)
	return out

func _process(_delta: float) -> void:
	var off := -fposmod(Time.get_ticks_msec() * 0.001 * SPEED, 1.0)
	for m in _mats:
		m.uv1_offset.y = off
