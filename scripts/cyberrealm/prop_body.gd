extends RigidBody3D
## Objet de décor physique (créé par props.gd) : clic gauche = prendre, puis clic gauche = lancer, clic droit =
## poser (lancer / poser n'exigent pas de viser l'objet tenu). Heurté assez fort (vitesse d'impact > BREAK_SPEED), il se brise en éclats.
##
## Trois modes (net_mode), choisis par props.gd :
##   LOCAL  — solo / hôte LAN : la physique est locale à la machine du joueur qui agit (état non synchronisé).
##   SERVER — serveur dédié : AUTORITÉ. Simule l'objet ; les clients lui envoient des demandes (toucher, prendre,
##            tenir, lancer, lâcher) et reçoivent les transformations (synchro de niveau) et les casses.
##   CLIENT — joueur sur un serveur dédié : le corps est un miroir cinématique collé au mesh (que la synchro de
##            niveau déplace) ; il ne sert qu'aux collisions locales et à la visée. Toute action est une demande.

enum { LOCAL, SERVER, CLIENT }

const HOLD_DIST := 1.1            # m devant la caméra
const HOLD_GAIN := 16.0           # raideur du suivi (vitesse = écart * gain)
const HOLD_MAX_SPEED := 12.0
const LOSE_DIST := 2.2            # m : l'objet est resté coincé loin de la main -> on le lâche
const THROW_SPEED := 11.0
const BREAK_SPEED := 8.5          # m/s de vitesse d'impact (composante normale) pour casser un objet
const SHARDS := 7
const MAX_PUSH_IMPULSE := 8.0     # serveur : borne d'une poussée demandée par un client
const MAX_THROW_SPEED := 14.0
const HOLD_STALE_MSEC := 600      # serveur : le client n'envoie plus sa cible de prise -> on lâche

var net_mode := LOCAL
var net_id := -1                  # indice du candidat dans props.gd (identique sur serveur et clients)
var props: Node = null            # le nœud Props (messages réseau, enregistrement de synchro)
var mesh_node: MeshInstance3D     # mesh d'origine (LOCAL/SERVER : suit le corps ; CLIENT : le corps le suit)
var _holder: Camera3D = null      # LOCAL / CLIENT : caméra du joueur qui tient l'objet
var _player: CharacterBody3D = null
var _v1 := Vector3.ZERO
var _v2 := Vector3.ZERO
var _broken := false
var _holder_peer := 0             # SERVER : pair qui tient l'objet (0 = personne)
var _hold_target := Vector3.ZERO
var _hold_stamp := 0
var _hold_tick := 0               # CLIENT : cadence d'envoi de la cible de prise
var _last_touch_msec := 0
# Dernier lancer (tous objets) : le clic qui lance ne doit pas aussi reprendre l'objet (ou un autre) via le
# raycast de world_interactor, qui traite le même clic dans sa propre frame.
static var _throw_msec := -1000
var _grab_msec := -1000           # le clic qui prend peut encore être « just pressed » au pas physique suivant

func _ready() -> void:
	contact_monitor = net_mode != CLIENT
	max_contacts_reported = 4
	add_to_group("prop_body")

# Intérieur désactivé (porte, waypoint : process_mode DISABLED) ou caché alors que l'objet est en main :
# _physics_process ne tourne plus et ne pourrait jamais le lâcher -> on le lâche ici, sur place.
func _notification(what: int) -> void:
	if (what == NOTIFICATION_DISABLED or what == NOTIFICATION_VISIBILITY_CHANGED) \
			and _holder != null and not (is_inside_tree() and can_process() and is_visible_in_tree()):
		_release()
		if net_mode == CLIENT and props != null and is_instance_valid(props):
			props.call("net_send", "prop_drop", net_id)

func get_interact_prompt() -> String:
	return "Left click: throw   Right click: drop" if _holder != null else "Left click: take"

func interact(player_id: int) -> void:
	if player_id != multiplayer.get_unique_id() or _broken:
		return
	if Time.get_ticks_msec() - _throw_msec < 150:
		return
	if _holder != null:
		_throw()
	else:
		_grab()

## Appelé par le joueur quand il percute l'objet encore figé : la physique s'enclenche s'il y a un support dessous
## (sinon c'est un objet posé sur un meuble sans collider : il reste du décor). `impulse` : poussée que le joueur
## aurait donnée (sert aux modes réseau, où c'est le serveur qui l'applique).
func touch(impulse := Vector3.ZERO) -> void:
	if _broken:
		return
	match net_mode:
		CLIENT:
			var now := Time.get_ticks_msec()
			if now - _last_touch_msec >= 80:
				_last_touch_msec = now
				props.call("net_send", "prop_touch", [net_id, impulse])
		SERVER:
			server_touch(impulse)
		_:
			if not freeze:
				return
			if _has_support():
				freeze = false

func _has_support() -> bool:
	var from := global_position + Vector3.UP * 0.05
	var q := PhysicsRayQueryParameters3D.create(from, from + Vector3.DOWN * 0.4, 1, [get_rid()])
	return not get_world_3d().direct_space_state.intersect_ray(q).is_empty()

func _head_camera() -> Camera3D:
	# Caméra « tête » du joueur (pas la caméra courante : en vue 3e personne elle est reculée derrière lui).
	var pl := get_tree().get_first_node_in_group("player")
	var cam := (pl.get_node_or_null("Camera3D") if pl != null else null) as Camera3D
	return cam if cam != null else get_viewport().get_camera_3d()

func _grab() -> void:
	var cam := _head_camera()
	if cam == null:
		return
	# Un seul objet en main.
	for b in get_tree().get_nodes_in_group("prop_body"):
		if b != self and b.get("_holder") != null:
			return
	_holder = cam
	_grab_msec = Time.get_ticks_msec()
	add_to_group("interact_held")      # world_interactor affiche « throw / drop » même sans viser l'objet
	var n: Node = cam
	while n != null and not (n is CharacterBody3D):
		n = n.get_parent()
	_player = n as CharacterBody3D
	if _player != null:
		add_collision_exception_with(_player)
	if net_mode == CLIENT:
		props.call("net_send", "prop_grab", net_id)     # le serveur arbitre (refus si déjà tenu)
		return
	freeze = false
	sleeping = false
	gravity_scale = 0.0

func _release() -> void:
	if _holder == null:
		return
	_holder = null
	remove_from_group("interact_held")
	if net_mode != CLIENT:
		gravity_scale = 1.0
	if _player != null and is_instance_valid(_player):
		remove_collision_exception_with(_player)
	_player = null

## CLIENT : le serveur a refusé la prise (objet déjà tenu par un autre joueur).
func net_grab_denied() -> void:
	_release()

func _throw() -> void:
	var cam := _holder
	if cam == null or not is_instance_valid(cam):
		_release()
		return
	_throw_msec = Time.get_ticks_msec()
	var pv := Vector3.ZERO
	if _player != null and is_instance_valid(_player):
		pv = _player.velocity
	_release()
	var dir := -cam.global_basis.z
	var vel := dir * THROW_SPEED + pv + Vector3.UP * 0.8
	var ang := Vector3(randf_range(-6, 6), randf_range(-6, 6), randf_range(-6, 6))
	if net_mode == CLIENT:
		props.call("net_send", "prop_throw", [net_id, vel, ang])
		return
	linear_velocity = vel
	angular_velocity = ang

func _physics_process(delta: float) -> void:
	match net_mode:
		SERVER:
			_server_hold()
		CLIENT:
			_client_hold()
		_:
			_local_hold()

func _local_hold() -> void:
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
	if _throw_pressed():
		_throw()
		return
	linear_velocity = (d * HOLD_GAIN).limit_length(HOLD_MAX_SPEED)
	angular_velocity *= 0.8

# Clic gauche avec un objet en main = lancer, où que vise le viseur (souris capturée : pas de menu ouvert).
func _throw_pressed() -> bool:
	return Time.get_ticks_msec() - _grab_msec > 150 and Input.mouse_mode == Input.MOUSE_MODE_CAPTURED \
			and Input.is_action_just_pressed("left_click", false)

# CLIENT : envoie la cible de prise au serveur (20 Hz) ; le serveur déplace l'objet, la synchro nous le ramène.
func _client_hold() -> void:
	if _holder == null:
		return
	if not is_instance_valid(_holder) or Input.is_action_just_pressed("right_click", true):
		_release()
		props.call("net_send", "prop_drop", net_id)
		return
	if _throw_pressed():
		_throw()
		return
	_hold_tick += 1
	if _hold_tick % 3 == 0:
		var target := _holder.global_position - _holder.global_basis.z * HOLD_DIST
		props.call("net_send", "prop_hold", [net_id, target], false)

# ── SERVEUR ──────────────────────────────────────────────────────────

func server_touch(impulse: Vector3) -> void:
	if _broken or _holder_peer != 0:
		return
	if freeze:
		if not _has_support():
			return
		freeze = false
		props.call("srv_awake", self)
	if impulse != Vector3.ZERO:
		sleeping = false
		apply_central_impulse(impulse.limit_length(MAX_PUSH_IMPULSE))

func server_grab(peer: int) -> bool:
	if _broken or (_holder_peer != 0 and _holder_peer != peer):
		return false
	_holder_peer = peer
	freeze = false
	sleeping = false
	gravity_scale = 0.0
	_hold_target = global_position
	_hold_stamp = Time.get_ticks_msec()
	props.call("srv_awake", self)
	return true

func server_hold(peer: int, target: Vector3) -> void:
	if peer == _holder_peer and peer != 0:
		_hold_target = target
		_hold_stamp = Time.get_ticks_msec()

func server_throw(peer: int, vel: Vector3, ang: Vector3) -> void:
	if peer != _holder_peer or peer == 0:
		return
	server_release(peer)
	linear_velocity = vel.limit_length(MAX_THROW_SPEED)
	angular_velocity = ang.limit_length(8.0)

func server_release(peer: int) -> void:
	if peer == _holder_peer or peer == -1:
		_holder_peer = 0
		gravity_scale = 1.0

func _server_hold() -> void:
	if _holder_peer == 0:
		return
	var d := _hold_target - global_position
	if Time.get_ticks_msec() - _hold_stamp > HOLD_STALE_MSEC or d.length() > LOSE_DIST * 2.0:
		server_release(_holder_peer)
		return
	linear_velocity = (d * HOLD_GAIN).limit_length(HOLD_MAX_SPEED)
	angular_velocity *= 0.8

func _integrate_forces(state: PhysicsDirectBodyState3D) -> void:
	# Casse selon la VITESSE d'impact seulement (lancé, tombé, projeté…), jamais selon la distance parcourue.
	if not _broken and _holder == null and _holder_peer == 0:
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
	if net_mode == SERVER:
		props.call("srv_break", net_id, vel)       # cache le mesh, prévient les clients, libère ce corps
		return
	spawn_shards(get_parent(), mesh_node, global_position, vel)
	queue_free()

## Éclats purement visuels (sans collision avec le joueur) ; cache le mesh d'origine.
static func spawn_shards(parent: Node, mesh_node: MeshInstance3D, origin: Vector3, vel: Vector3) -> void:
	var mat: Material = null
	var size := 0.1
	if mesh_node != null and is_instance_valid(mesh_node):
		mat = mesh_node.get_active_material(0)
		size = clampf(mesh_node.get_aabb().size.length() * mesh_node.global_transform.basis.get_scale().x, 0.06, 0.5)
		mesh_node.visible = false
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
