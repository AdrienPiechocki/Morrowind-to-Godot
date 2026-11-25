extends StaticBody3D
## Porte cliquable : attaché par interiors.gd au collider du mesh de la porte.
## Le clic gauche appelle interact() sur TOUTES les machines de la session
## (world_interactor) ; interiors.gd ne téléporte que le joueur dont l'id
## correspond à celui qui a cliqué.
##
## Porte sans destination (data.kind == "swing") : elle s'ouvre / se ferme sur sa
## charnière (l'origine du mesh) à chaque clic, sur chaque machine. Le sens
## d'ouverture est déterministe (côté dégagé) pour que tout le monde voie la même.

const OPEN_ANGLE := PI * 0.5
const OPEN_TIME := 0.45
const PROBE_RADIUS := 0.25

var manager: Node = null
var forward: Node = null   # zone de visée (PickProxy) : renvoie vers la porte
var prompt := ""
var data := {}      # cible/sens de la porte (runtime, jamais sérialisé)

var _open := false
var _sign := 0.0
var _closed_mesh := Transform3D.IDENTITY
var _closed_body := Transform3D.IDENTITY
var _tween: Tween = null
var _angle := 0.0
var _axis := Vector3.UP   # axe de la charnière (repère local) : Y pour un battant, X pour une trappe à plat

func interact(player_id: int) -> void:
	if forward != null and is_instance_valid(forward):
		forward.call("interact", player_id)
		return
	if data.get("kind", "") == "swing":
		_toggle()
	elif manager != null and is_instance_valid(manager):
		manager.use_door(self, player_id)

func get_interact_prompt() -> String:
	if forward != null and is_instance_valid(forward):
		return forward.call("get_interact_prompt")
	if data.get("kind", "") == "swing":
		return "Close door" if _open else "Open door"
	return prompt

func _toggle() -> void:
	var mesh := data.get("mesh") as Node3D
	if mesh == null or not is_instance_valid(mesh):
		return
	if _sign == 0.0:
		_closed_mesh = mesh.transform
		_closed_body = transform
		_axis = _hinge_axis(mesh)
		_sign = _lift_side(mesh) if _axis != Vector3.UP else _free_side(mesh)
	_open = not _open
	var target := _sign * OPEN_ANGLE if _open else 0.0
	if _tween != null and _tween.is_valid():
		_tween.kill()
	var from := _angle
	_tween = create_tween()
	_tween.tween_method(_set_angle.bind(mesh), from, target, OPEN_TIME * absf(target - from) / OPEN_ANGLE) \
		.set_trans(Tween.TRANS_SINE).set_ease(Tween.EASE_IN_OUT)

# Pivote le mesh ET son collider (frères, même repère) autour de l'axe local de la charnière.
func _set_angle(a: float, mesh: Node3D) -> void:
	if not is_instance_valid(mesh):
		return
	_angle = a
	var rot := Basis(_axis, a)
	mesh.transform = Transform3D(_closed_mesh.basis * rot, _closed_mesh.origin)
	transform = Transform3D(_closed_body.basis * rot, _closed_body.origin)

# Côté vers lequel le battant s'ouvre : celui où l'extrémité libre ne rencontre
# aucun mur. Calcul sur la géométrie statique : identique sur toutes les machines.
func _free_side(mesh: Node3D) -> float:
	var mi := mesh as MeshInstance3D
	if mi == null or mi.mesh == null:
		return 1.0
	var c := mi.mesh.get_aabb().get_center()
	var space := get_world_3d().direct_space_state
	var shape := SphereShape3D.new()
	shape.radius = PROBE_RADIUS
	for s in [1.0, -1.0]:
		var local_end := Basis(Vector3.UP, s * OPEN_ANGLE) * c
		var q := PhysicsShapeQueryParameters3D.new()
		q.shape = shape
		q.collision_mask = 1
		q.exclude = [get_rid()]
		q.transform = Transform3D(Basis.IDENTITY, mesh.global_transform * local_end)
		if space.intersect_shape(q, 1).is_empty():
			return s
	return 1.0

# Panneau à plat (fin sur Y, ex. trappe de comptoir « bar_door ») : charnière sur son bord, axe X local.
# Les battants verticaux gardent l'axe Y.
func _hinge_axis(mesh: Node3D) -> Vector3:
	var mi := mesh as MeshInstance3D
	if mi == null or mi.mesh == null:
		return Vector3.UP
	var sz := mi.mesh.get_aabb().size
	if sz.y < sz.x and sz.y < sz.z:
		return Vector3.RIGHT
	return Vector3.UP

# Sens qui soulève la trappe (et non qui l'enfonce dans le comptoir) ; déterministe.
func _lift_side(mesh: Node3D) -> float:
	var mi := mesh as MeshInstance3D
	var c := mi.mesh.get_aabb().get_center()
	var up_pos: float = (mesh.global_basis * (Basis(_axis, OPEN_ANGLE) * c)).y
	var up_neg: float = (mesh.global_basis * (Basis(_axis, -OPEN_ANGLE) * c)).y
	return 1.0 if up_pos >= up_neg else -1.0
