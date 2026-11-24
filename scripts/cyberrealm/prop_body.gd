extends RigidBody3D
## Objet de décor physique (créé par props.gd) : clic gauche = prendre, puis clic gauche = lancer, clic droit =
## poser. Heurté assez fort (vitesse d'impact > BREAK_SPEED), il se brise en éclats. Tout est LOCAL à la machine du joueur qui
## agit (interact() est relayé en LAN mais seul l'appelant, pid == son propre id, réagit) : comme le reste de la
## physique du décor, l'état n'est pas synchronisé.

const HOLD_DIST := 1.1            # m devant la caméra
const HOLD_GAIN := 16.0           # raideur du suivi (vitesse = écart * gain)
const HOLD_MAX_SPEED := 12.0
const LOSE_DIST := 2.2            # m : l'objet est resté coincé loin de la main -> on le lâche
const THROW_SPEED := 11.0
const BREAK_SPEED := 8.5          # m/s de vitesse d'impact (composante normale) pour casser un objet
const SHARDS := 7

var mesh_node: MeshInstance3D     # mesh d'origine, qui suit le corps (RemoteTransform3D)
var _holder: Camera3D = null
var _player: CharacterBody3D = null
var _v1 := Vector3.ZERO
var _v2 := Vector3.ZERO
var _broken := false

func _ready() -> void:
	contact_monitor = true
	max_contacts_reported = 4
	add_to_group("prop_body")

func get_interact_prompt() -> String:
	return "Left click: throw   Right click: drop" if _holder != null else "Left click: take"

func interact(player_id: int) -> void:
	if player_id != multiplayer.get_unique_id() or _broken:
		return
	if _holder != null:
		_throw()
	else:
		_grab()

## Appelé par le joueur quand il percute l'objet encore figé : la physique s'enclenche s'il y a un support dessous
## (sinon c'est un objet posé sur un meuble sans collider : il reste du décor).
func touch() -> void:
	if not freeze or _broken:
		return
	var from := global_position + Vector3.UP * 0.05
	var q := PhysicsRayQueryParameters3D.create(from, from + Vector3.DOWN * 0.4, 1, [get_rid()])
	if not get_world_3d().direct_space_state.intersect_ray(q).is_empty():
		freeze = false

func _grab() -> void:
	# Caméra « tête » du joueur (pas la caméra courante : en vue 3e personne elle est reculée derrière lui).
	var pl := get_tree().get_first_node_in_group("player")
	var cam := (pl.get_node_or_null("Camera3D") if pl != null else null) as Camera3D
	if cam == null:
		cam = get_viewport().get_camera_3d()
	if cam == null:
		return
	# Un seul objet en main.
	for b in get_tree().get_nodes_in_group("prop_body"):
		if b != self and b.get("_holder") != null:
			return
	_holder = cam
	var n: Node = cam
	while n != null and not (n is CharacterBody3D):
		n = n.get_parent()
	_player = n as CharacterBody3D
	if _player != null:
		add_collision_exception_with(_player)
	freeze = false
	sleeping = false
	gravity_scale = 0.0

func _release() -> void:
	if _holder == null:
		return
	_holder = null
	gravity_scale = 1.0
	if _player != null and is_instance_valid(_player):
		remove_collision_exception_with(_player)
	_player = null

func _throw() -> void:
	var cam := _holder
	var pv := Vector3.ZERO
	if _player != null and is_instance_valid(_player):
		pv = _player.velocity
	_release()
	var dir := -cam.global_basis.z
	linear_velocity = dir * THROW_SPEED + pv + Vector3.UP * 0.8
	angular_velocity = Vector3(randf_range(-6, 6), randf_range(-6, 6), randf_range(-6, 6))

func _physics_process(delta: float) -> void:
	if _holder == null:
		return
	if not is_instance_valid(_holder) or not is_visible_in_tree() or Input.is_action_just_pressed("right_click", true):
		_release()
		return
	var target := _holder.global_position - _holder.global_basis.z * HOLD_DIST
	var d := target - global_position
	if d.length() > LOSE_DIST:
		_release()
		return
	linear_velocity = (d * HOLD_GAIN).limit_length(HOLD_MAX_SPEED)
	angular_velocity *= 0.8

func _integrate_forces(state: PhysicsDirectBodyState3D) -> void:
	# Casse selon la VITESSE d'impact seulement (lancé, tombé, projeté…), jamais selon la distance parcourue.
	if not _broken and _holder == null:
		var best := 0.0
		for i in state.get_contact_count():
			var other := state.get_contact_collider_object(i) as RigidBody3D
			if other != null and other != self and other.freeze and other.has_method("touch") \
					and maxf(_v1.length(), _v2.length()) > 1.0:
				call_deferred("_wake_other", other, _v1 if _v1.length() > _v2.length() else _v2)    # lancé sur un autre objet figé
			var n := state.get_contact_local_normal(i)
			var rel := state.get_contact_collider_velocity_at_position(i)
			best = maxf(best, maxf(absf((_v1 - rel).dot(n)), absf((_v2 - rel).dot(n))))
		if best > BREAK_SPEED:
			_broken = true
			call_deferred("_shatter", state.linear_velocity)
	_v2 = _v1
	_v1 = state.linear_velocity

func _wake_other(other: RigidBody3D, vel: Vector3) -> void:
	if not is_instance_valid(other):
		return
	other.call("touch")
	if not other.freeze:
		other.apply_central_impulse(vel * other.mass * 0.4)

func _shatter(vel: Vector3) -> void:
	_release()
	var origin := global_position
	var mat: Material = null
	var size := 0.1
	if mesh_node != null and is_instance_valid(mesh_node):
		mat = mesh_node.get_active_material(0)
		size = clampf(mesh_node.get_aabb().size.length() * mesh_node.global_transform.basis.get_scale().x, 0.06, 0.5)
		mesh_node.visible = false
	var parent := get_parent()
	for i in SHARDS:
		var s := RigidBody3D.new()
		s.collision_layer = 0              # n'entrent en collision qu'avec le décor, jamais avec le joueur
		s.collision_mask = 1
		s.mass = 0.05
		var bm := BoxMesh.new()
		bm.size = Vector3(randf_range(0.3, 0.8), randf_range(0.1, 0.3), randf_range(0.3, 0.8)) * size * 0.35
		bm.material = mat
		var mi := MeshInstance3D.new()
		mi.mesh = bm
		s.add_child(mi)
		var cs := CollisionShape3D.new()
		var sh := BoxShape3D.new()
		sh.size = bm.size
		cs.shape = sh
		s.add_child(cs)
		parent.add_child(s)
		s.global_position = origin + Vector3(randf_range(-1, 1), randf_range(-0.5, 1), randf_range(-1, 1)) * size * 0.25
		s.global_basis = Basis.from_euler(Vector3(randf(), randf(), randf()) * TAU)
		s.linear_velocity = vel * 0.3 + Vector3(randf_range(-1, 1), randf_range(0.5, 2.5), randf_range(-1, 1)) * 1.5
		s.angular_velocity = Vector3(randf_range(-8, 8), randf_range(-8, 8), randf_range(-8, 8))
		var tw := s.create_tween()
		tw.tween_interval(randf_range(3.0, 5.0))
		tw.tween_property(s, "scale", Vector3.ONE * 0.01, 1.0)
		tw.tween_callback(s.queue_free)
	queue_free()
