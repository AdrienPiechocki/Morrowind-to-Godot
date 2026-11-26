extends Node
## Décor vivant du niveau Morrowind, ajouté À L'EXÉCUTION (rien n'est écrit dans les .gltf) :
##   - lanternes / lampes : lumière chaude + halo émissif (extérieur : seulement quand le soleil est bas) ;
##   - lanternes suspendues : léger balancement (pendule autour du point d'accroche) ;
##   - petits objets sans collision (bouteilles, bols, livres…) : RigidBody3D que le joueur peut pousser.
## Les objets physiques se prennent / lancent / cassent (prop_body.gd).
## Sur un SERVEUR DÉDIÉ la physique des objets est simulée par le serveur (autorité) : les clients envoient des
## demandes (messages de niveau « prop_* »), le serveur diffuse les transformations (synchro de niveau du lan_manager,
## via la méta « lan_sync_dynamic » des meshes) et les casses. L'identifiant d'un objet est son indice dans _bodies,
## identique partout puisque serveur et clients parcourent le même niveau (même blob) dans le même ordre.
## Les nœuds créés n'ont pas d'owner et leur parent porte « lan_skip_children » : le LevelBaker les ignore,
## chaque machine les reconstruit au chargement (comme les portes). CYBERREALM_NO_PROPS=1 désactive tout.

const PropBodyScript := preload("res://user/Levels/Morrowind/scripts/prop_body.gd")

const PROP_LAYER := 1 << 20                # couche physique des objets de décor (voir player.gd)

# Seules les sources AVEC émission reçoivent une lumière (lightpost, torch… ont un support : lumière mal placée).
const GLOW_WORDS := ["lantern", "lamp", "sconce", "chandelier"]
const NO_GLOW_NAMES := ["light_de_lamp"]                         # sans lumière ni émission
const SWAY_WORDS := ["lantern"]                                     # suspendues
# Tout objet SANS collider devient physique, sauf la flore, la structure et le décor souple/lumineux.
const NOT_PHYS_WORDS := ["-colonly", "plant", "flora", "grass", "tree_", "_tree", "bush", "shrub", "leaf", "leaves",
	"fern", "flower", "kelp", "weed", "reed", "moss", "mushroom", "fungus", "vine", "root", "stalk", "bloom", "petal",
	"lichen", "cactus", "coral", "bramble", "heather", "wickwheat", "terrain", "land_",
	"wall", "floor", "flr", "door", "stair", "ceil", "beam", "arch_", "archway", "pillar", "column", "bridge",
	"rock", "boulder", "rug", "rope", "banner", "curtain", "tapestry", "web", "waterfall", "watersurface", "fire_",
	"fireplace", "smoke", "steam", "lantern", "lamp", "torch", "signpost", "signbase", "active_sign", "post",
	"rack", "hook", "coals", "practice_mat",
	"var_blood", "decal"]                     # flaques de sang (t_glb_var_blood_*), décalques muraux : peints au sol/mur
# Étagères : leur collider d'origine (« -colonly ») est une boîte pleine qui englobe les rayons. Les objets posés
# dessus étaient enfermés dedans (rayon de visée bloqué, corps éjectés au réveil) : il est remplacé par le mesh visible.
const SHELF_WORDS := ["shelf"]
const ITEM_PREFIXES := ["ingred_", "bk_", "p_", "sc_", "misc_"]    # objets ramassables : priment sur les mots exclus
const NOT_PHYS_PREFIXES := ["in_", "ex_"]                          # modules d'architecture Morrowind
# Caisses, urnes : statiques, avec collision en boîte (noms exacts relevés dans l'export Morrowind).
const STATIC_BOX_WORDS := ["crate", "furn_u", "_cr1", "_cr2", "_cr3", "_cr4", "_cr5", "urn_"]
const PHYS_MIN_SIZE := 0.015               # m : plus petit = poussière
const PHYS_MAX_SIZE := 1.6                # m : plus grand = meuble
const STATIC_NEAR := 25.0                # m : collisions statiques (caisses, urnes) créées autour de la caméra
const STATIC_FAR := 35.0
const BODY_NEAR := 5.0                   # m : un corps (figé) est créé quand le joueur est plus près
const BODY_FAR := 8.0                    # m : et détruit au-delà, tant qu'il n'a pas été touché
const SWAY_RADIUS := 40.0                 # m : balancement actif autour de la caméra
const LIGHT_RANGE := 7.0
const LIGHT_ENERGY := 0.7
const LIGHT_COLOR := Color(1.0, 0.68, 0.38)
const FADE_BEGIN := 70.0                  # m : la lumière s'éteint progressivement (perf iGPU)
const FADE_LENGTH := 30.0
const INDOOR_MIN_Y := 3000.0
# Serveur : distances maximales joueur -> objet pour accepter une demande (portée de visée 3 m + marge de latence).
const SRV_GRAB_REACH := 4.5
const SRV_TOUCH_REACH := 3.5
const SRV_HOLD_REACH := 4.0             # la cible de prise doit rester près du joueur

enum { NET_LOCAL, NET_SERVER, NET_CLIENT }
var _net := NET_LOCAL
var _lan: Node = null

var _lights: Array = []        # {light, base, outdoor}
var _sway: Array = []          # {mi, base, pivot, gpos, amp, freq, phase}
var _statics: Array = []       # {mi, gpos, body}
var _bodies: Array = []        # {mi, gpos, home, body (null si non instancié), woken}
var _t := 0.0
var _acc := 0.0
var _sun: Light3D
var _mi_seen := {}

func setup(roots: Array) -> void:
	if OS.get_environment("CYBERREALM_NO_PROPS") == "1":
		return
	_setup_net()
	var level := get_parent().get_parent()
	_sun = level.get_node_or_null("Sun") as Light3D if level != null else null
	for r in roots:
		if r == null:
			continue
		for n in (r as Node).find_children("*", "MeshInstance3D", true, false):
			_process_mesh(n as MeshInstance3D)

static func _has_word(s: String, words: Array) -> bool:
	for w in words:
		if s.contains(w):
			return true
	return false

static func _starts_with_any(s: String, prefixes: Array) -> bool:
	for p in prefixes:
		if s.begins_with(p):
			return true
	return false

func _has_collider(mi: MeshInstance3D) -> bool:
	return _collider_of(mi) != null

# Collider importé d'un mesh (le « -colonly » de l'import glTF, frère juste après, même origine).
func _collider_of(mi: MeshInstance3D) -> StaticBody3D:
	var par := mi.get_parent()
	for i in range(mi.get_index() + 1, mini(mi.get_index() + 3, par.get_child_count())):
		var sb := par.get_child(i) as StaticBody3D
		if sb != null and sb.global_position.distance_to(mi.global_position) < 0.05:
			return sb
	return null

# Remplace la boîte pleine d'une étagère par la géométrie du mesh visible (planches, montants) : les rayons de
# visée passent entre les planches jusqu'aux objets, qui reposent sur les planches. Idempotent (rejoué sur un
# niveau déjà traité, ex. reçu en LAN, il reconstruit la même forme).
func _fit_shelf_collider(mi: MeshInstance3D) -> void:
	var sb := _collider_of(mi)
	if sb == null:
		return
	var faces := mi.mesh.get_faces()
	if faces.is_empty():
		return
	var xf := sb.global_transform.affine_inverse() * mi.global_transform
	for i in faces.size():
		faces[i] = xf * faces[i]
	var shape := ConcavePolygonShape3D.new()
	shape.backface_collision = true        # planches souvent simple face
	shape.set_faces(faces)
	var done := false
	for c in sb.get_children():
		var cs := c as CollisionShape3D
		if cs == null:
			continue
		if not done:
			cs.shape = shape
			cs.transform = Transform3D.IDENTITY
			cs.disabled = false
			done = true
		else:
			cs.disabled = true

func _setup_net() -> void:
	_lan = get_node_or_null("/root/WaylandRoom/LAN")
	if _lan == null or OS.get_environment("CYBERREALM_LOCAL_PROPS") == "1":
		return
	if _lan.get("dedicated") == true:
		_net = NET_SERVER
	elif _lan.get("session_dedicated") == true:
		_net = NET_CLIENT
	if _net == NET_LOCAL:
		return
	_lan.level_message.connect(_on_level_message)
	if _net == NET_SERVER:
		_lan.level_peer_left.connect(_on_peer_left)
	# Instantané de l'état des objets (joueurs qui arrivent, persistance du serveur) : cf. level_sync.gd de CyberRealm.
	add_to_group("net_snapshot")

func _process_mesh(mi: MeshInstance3D) -> void:
	if mi.mesh == null or _mi_seen.has(mi):
		return
	_mi_seen[mi] = true
	var low := String(mi.name).to_lower()
	if _has_word(low, NO_GLOW_NAMES):
		return
	if _has_word(low, SHELF_WORDS) and low.contains("furn"):    # (pas « flora_bc_shelffungus »)
		_fit_shelf_collider(mi)
		return
	if _has_word(low, GLOW_WORDS) and not low.contains("-colonly"):
		if _net != NET_SERVER:       # (un serveur n'a ni lumière ni balancement : pas de rendu)
			_add_glow(mi, low)
			if _has_word(low, SWAY_WORDS):
				_add_sway(mi, low)
	elif (_has_word(low, STATIC_BOX_WORDS) or low.begins_with("urn")) and not low.contains("-colonly"):
		_add_static_box(mi)
	elif (_starts_with_any(low, ITEM_PREFIXES) or not _has_word(low, NOT_PHYS_WORDS)) \
			and not _starts_with_any(low, NOT_PHYS_PREFIXES):
		_add_rigid(mi)

# ── Lumière ──────────────────────────────────────────────────────────

func _add_glow(mi: MeshInstance3D, low: String) -> void:
	var light := OmniLight3D.new()
	light.name = "PropLight"
	light.light_color = LIGHT_COLOR
	light.light_energy = LIGHT_ENERGY
	light.omni_range = LIGHT_RANGE * (1.4 if low.contains("lightpost") else 1.0)
	light.shadow_enabled = false
	light.light_size = 1.0               # rayon de la source (ombres/spéculaire plus doux)
	light.omni_attenuation = 0.5
	light.distance_fade_enabled = true
	light.distance_fade_begin = FADE_BEGIN
	light.distance_fade_length = FADE_LENGTH
	light.light_cull_mask = mi.layers | 1       # couche du mesh (extérieur / intérieur) + avatars
	light.position = mi.get_aabb().get_center()
	mi.set_meta("lan_skip_children", true)
	mi.add_child(light)
	var outdoor := mi.global_position.y < INDOOR_MIN_Y
	_lights.append({"light": light, "base": LIGHT_ENERGY, "outdoor": outdoor})

# Extérieur : allumées quand le soleil/la lune est faible. Appelé quelques fois par seconde.
func _update_lights() -> void:
	var dark := 1.0
	if _sun != null:
		dark = 1.0 - smoothstep(0.1, 0.9, _sun.light_energy)
	for d in _lights:
		var l := d.light as OmniLight3D
		if not is_instance_valid(l):
			continue
		if d.outdoor:
			l.visible = dark > 0.02
			l.light_energy = d.base * dark
		else:
			l.visible = true

# ── Balancement ──────────────────────────────────────────────────────

func _add_sway(mi: MeshInstance3D, low: String) -> void:
	var aabb := mi.get_aabb()
	_sway.append({
		"mi": mi, "base": mi.transform,
		"pivot": Vector3(aabb.get_center().x, aabb.end.y, aabb.get_center().z),   # point d'accroche : haut du mesh
		"gpos": mi.global_position,
		"amp": deg_to_rad(5.0 if low.contains("sway") else 2.5),
		"freq": randf_range(1.3, 1.9), "phase": randf() * TAU})

func _update_sway() -> void:
	var cam := get_viewport().get_camera_3d()
	if cam == null:
		return
	var cp := cam.global_position
	for d in _sway:
		var mi := d.mi as MeshInstance3D
		if not is_instance_valid(mi) or (d.gpos as Vector3).distance_squared_to(cp) > SWAY_RADIUS * SWAY_RADIUS \
				or not mi.is_visible_in_tree():
			continue
		var a: float = d.amp * (sin(_t * d.freq + d.phase) + 0.4 * sin(_t * d.freq * 2.3 + d.phase * 1.7))
		var b: float = d.amp * 0.7 * sin(_t * d.freq * 0.83 + d.phase * 2.1)
		var rot := Basis(Vector3.RIGHT, a) * Basis(Vector3.BACK, b)
		var p: Vector3 = d.pivot
		mi.transform = (d.base as Transform3D) * Transform3D(Basis.IDENTITY, p) * Transform3D(rot, Vector3.ZERO) \
				* Transform3D(Basis.IDENTITY, -p)

# ── Objets physiques ─────────────────────────────────────────────────
# Les ~4000 objets du niveau ne sont PAS tous des corps physiques (limite Jolt, temps de chargement) : au
# chargement on ne fait que repérer les candidats ; le corps (figé, boîte englobante) n'est créé que quand le
# joueur est à moins de BODY_NEAR m et détruit au-delà de BODY_FAR m tant qu'il n'a pas été touché. Figé, il ne
# bouge pas : la physique ne s'enclenche que quand le joueur le touche (touch(), appelé par player.gd) ou le prend.

func _add_rigid(mi: MeshInstance3D) -> void:
	var sc := mi.global_transform.basis.get_scale().abs()
	var size := mi.get_aabb().size * sc
	var ext := maxf(size.x, maxf(size.y, size.z))
	if ext < PHYS_MIN_SIZE or ext > PHYS_MAX_SIZE or _has_collider(mi) \
			or (ext > 0.6 and String(mi.name).to_lower().contains("furn_")):     # meubles : pas des objets
		return
	_bodies.append({"mi": mi, "gpos": mi.global_position, "home": mi.global_transform, "body": null, "woken": false,
			"broken": false, "synced": false, "id": _bodies.size()})
	if _net != NET_LOCAL:
		mi.set_meta("lan_sync_dynamic", true)     # le client accepte les transformations du serveur pour ce mesh

# Collision statique en boîte englobante (caisses, urnes sans collider d'origine). Créée à la demande autour de la
# caméra (limite de corps de Jolt : il y en a des milliers dans le niveau).
func _add_static_box(mi: MeshInstance3D) -> void:
	if _has_collider(mi):
		return
	_statics.append({"mi": mi, "gpos": mi.global_position, "body": null})

func _make_static(d: Dictionary) -> void:
	var mi := d.mi as MeshInstance3D
	var sc := mi.global_transform.basis.get_scale().abs()
	var aabb := mi.get_aabb()
	var box := BoxShape3D.new()
	box.size = aabb.size * sc
	var sb := StaticBody3D.new()
	sb.name = "PropStatic"
	var cs := CollisionShape3D.new()
	cs.shape = box
	cs.position = aabb.get_center() * sc
	sb.add_child(cs)
	var par := mi.get_parent()
	par.set_meta("lan_skip_children", true)
	par.add_child(sb)
	sb.global_transform = Transform3D(mi.global_transform.basis.orthonormalized(), mi.global_position)
	d.body = sb

func _update_statics(centers: Array, near := STATIC_NEAR, far := STATIC_FAR) -> void:
	var near2 := near * near
	var far2 := far * far
	for d in _statics:
		var sb = d.body
		if sb != null and not is_instance_valid(sb):
			d.body = null
			sb = null
		var d2 := _min_dist2(d.gpos, centers)
		if sb == null:
			if d2 < near2 and is_instance_valid(d.mi) and _usable(d.mi):
				_make_static(d)
		elif d2 > far2:
			sb.queue_free()
			d.body = null

static func _cur_pos(d: Dictionary) -> Vector3:
	return (d.mi as Node3D).global_position if is_instance_valid(d.mi) else d.gpos

static func _min_dist2(p: Vector3, centers: Array) -> float:
	var m := INF
	for c in centers:
		m = minf(m, p.distance_squared_to(c))
	return m

# Un serveur ignore la visibilité (les intérieurs y sont cachés mais bien simulés).
func _usable(mi: MeshInstance3D) -> bool:
	return _net == NET_SERVER or mi.is_visible_in_tree()

func _make_body(d: Dictionary) -> void:
	var mi := d.mi as MeshInstance3D
	var sc := mi.global_transform.basis.get_scale().abs()
	var aabb := mi.get_aabb()
	var size := aabb.size * sc
	var shape := BoxShape3D.new()
	shape.size = size.max(Vector3.ONE * 0.02)
	var body := RigidBody3D.new()
	body.set_script(PropBodyScript)
	body.set("mesh_node", mi)
	body.set("net_mode", int(_net))      # NET_LOCAL/SERVER/CLIENT = LOCAL/SERVER/CLIENT de prop_body.gd
	body.set("net_id", d.id)
	body.set("props", self)
	body.name = "PropBody"
	body.mass = clampf(size.x * size.y * size.z * 600.0, 0.1, 4.0)
	body.collision_layer = PROP_LAYER            # couche à part : le joueur ne subit jamais leur mouvement (player.gd)
	body.collision_mask = 1 | PROP_LAYER
	body.freeze = true
	body.freeze_mode = RigidBody3D.FREEZE_MODE_KINEMATIC if _net == NET_CLIENT else RigidBody3D.FREEZE_MODE_STATIC
	body.linear_damp = 0.4
	body.angular_damp = 1.5
	body.continuous_cd = true                    # petits objets lancés vite : ne traversent plus les murs fins
	var cs := CollisionShape3D.new()
	cs.shape = shape
	cs.position = aabb.get_center() * sc
	body.add_child(cs)
	var par := mi.get_parent()
	par.set_meta("lan_skip_children", true)
	par.add_child(body)
	body.global_transform = Transform3D(mi.global_transform.basis.orthonormalized(), mi.global_position)
	# Le mesh d'origine reste en place dans l'arbre (donc baké normalement) et suit le corps. Sauf CLIENT : c'est la
	# synchro du serveur qui déplace le mesh, et le corps (cinématique) le suit.
	if _net != NET_CLIENT:
		var rt := RemoteTransform3D.new()
		rt.update_scale = false
		body.add_child(rt)
		rt.remote_path = rt.get_path_to(mi)
	d.body = body

func _update_bodies() -> void:
	# Centres d'intérêt : la caméra du joueur ; sur un serveur, les objets en mouvement (personne n'y « regarde »).
	var centers: Array = []
	if _net == NET_SERVER:
		for d in _bodies:
			if d.body != null and is_instance_valid(d.body) and not (d.body as RigidBody3D).freeze:
				centers.append((d.body as RigidBody3D).global_position)
		_update_statics(centers, 8.0, 14.0)
	else:
		var cam := get_viewport().get_camera_3d()
		if cam == null:
			return
		centers = [cam.global_position]
		_update_statics(centers)
	var near2 := (3.0 if _net == NET_SERVER else BODY_NEAR) * (3.0 if _net == NET_SERVER else BODY_NEAR)
	var far2 := (6.0 if _net == NET_SERVER else BODY_FAR) * (6.0 if _net == NET_SERVER else BODY_FAR)
	for d in _bodies:
		if d.broken:
			continue
		var body = d.body
		if body != null and not is_instance_valid(body):
			d.body = null
			continue
		# Distance à la position ACTUELLE de l'objet, pas à sa place d'origine : chez un client, un objet qu'un autre
		# joueur a déplacé (synchro du serveur) n'avait de corps — donc ne pouvait être visé — que près de son origine.
		var pos := _cur_pos(d)
		if body != null:
			var rb := body as RigidBody3D
			if not rb.freeze:
				d.woken = true                       # touché / pris : il reste, indépendant de la distance
				if _net == NET_SERVER:
					_srv_sync_state(d, rb)
				if rb.global_position.y < (d.home as Transform3D).origin.y - 15.0:    # traversé le décor
					rb.freeze = true
					rb.global_transform = d.home
					d.woken = false
					_srv_unsync(d)
			elif not d.woken and _min_dist2(pos, centers) > far2:
				if _net != NET_CLIENT:      # (client : le mesh est placé par la synchro du serveur, jamais remis ici)
					(d.mi as MeshInstance3D).global_transform = d.home
				rb.queue_free()
				d.body = null
		# Client : aussi pour un objet déjà déplacé (woken, ex. par l'instantané reçu à l'arrivée) dont le corps n'existe
		# pas encore ici — sans corps, il ne pouvait être ni visé ni heurté.
		elif (not d.woken or _net == NET_CLIENT) and _min_dist2(pos, centers) < near2 \
				and is_instance_valid(d.mi) and _usable(d.mi):
			_make_body(d)

# ── Réseau : serveur ─────────────────────────────────────────────────

func _cand(id: Variant) -> Dictionary:
	if not (id is int) or id < 0 or id >= _bodies.size():
		return {}
	return _bodies[id]

# Appelé par un corps qui vient d'être réveillé : son mesh entre dans la synchro de transformations.
func srv_awake(body: RigidBody3D) -> void:
	var d := _cand(int(body.get("net_id")))
	if d.is_empty() or _lan == null:
		return
	d.synced = true
	_lan.register_level_sync(d.mi)

func _srv_unsync(d: Dictionary) -> void:
	if d.synced and _lan != null and is_instance_valid(d.mi):
		_lan.unregister_level_sync(d.mi)
	d.synced = false

# Un corps qui s'est endormi (et que personne ne tient) sort de la synchro ; réveillé, il y rentre.
func _srv_sync_state(d: Dictionary, rb: RigidBody3D) -> void:
	var held: bool = int(rb.get("_holder_peer")) != 0
	if rb.sleeping and not held:
		_srv_unsync(d)
	elif not d.synced:
		d.synced = true
		_lan.register_level_sync(d.mi)

# Casse décidée par le serveur : le mesh est caché, les clients jouent les éclats, le corps est libéré.
func srv_break(id: int, vel: Vector3) -> void:
	var d := _cand(id)
	if d.is_empty():
		return
	d.broken = true
	_srv_unsync(d)
	(d.mi as MeshInstance3D).visible = false
	if is_instance_valid(d.body):
		(d.body as Node).queue_free()
	d.body = null
	if _lan != null:
		_lan.broadcast_level_message("prop_break", [id, vel, (d.mi as MeshInstance3D).global_position])

func _srv_body_for(id: int) -> RigidBody3D:
	var d := _cand(id)
	if d.is_empty() or d.broken or not is_instance_valid(d.mi):
		return null
	if d.body == null or not is_instance_valid(d.body):
		# Collisions statiques voisines (caisses, urnes…) d'abord, pour que l'objet ne tombe pas à travers.
		_update_statics([_cur_pos(d)], 8.0, 14.0)
		_make_body(d)
	return d.body as RigidBody3D

func _on_level_message(topic: String, data: Variant, from: int) -> void:
	if _net == NET_SERVER:
		_srv_message(topic, data, from)
	elif _net == NET_CLIENT:
		_cl_message(topic, data)

## Le joueur `peer` est-il à moins de `reach` m de l'objet `id` ? (position connue du serveur, cf. lan.peer_within)
func _srv_near(peer: int, id: int, reach: float) -> bool:
	var d := _cand(id)
	if d.is_empty() or not is_instance_valid(d.mi):
		return false
	var pos: Vector3 = (d.body as Node3D).global_position if d.body != null and is_instance_valid(d.body) \
			else (d.mi as Node3D).global_position
	return not _lan.has_method("peer_within") or bool(_lan.peer_within(peer, pos, reach))

func _srv_message(topic: String, data: Variant, from: int) -> void:
	if not (data is Array or data is int):
		return
	match topic:
		"prop_touch":      # [id, impulse]
			if data is Array and data.size() == 2 and data[1] is Vector3 and _srv_near(from, int(data[0]), SRV_TOUCH_REACH):
				var b := _srv_body_for(int(data[0]))
				if b != null:
					b.call("server_touch", data[1])
		"prop_grab":       # id
			var b := _srv_body_for(int(data)) if _srv_near(from, int(data), SRV_GRAB_REACH) else null
			if b == null or not bool(b.call("server_grab", from)):
				_lan.send_level_message_to(from, "prop_grab_denied", int(data))
		"prop_hold":       # [id, target]
			if data is Array and data.size() == 2 and data[1] is Vector3 \
					and (not _lan.has_method("peer_within") or bool(_lan.peer_within(from, data[1], SRV_HOLD_REACH))):
				var d := _cand(int(data[0]))
				if not d.is_empty() and is_instance_valid(d.body):
					d.body.call("server_hold", from, data[1])
		"prop_throw":      # [id, vel, ang]
			if data is Array and data.size() == 3 and data[1] is Vector3 and data[2] is Vector3:
				var d := _cand(int(data[0]))
				if not d.is_empty() and is_instance_valid(d.body):
					d.body.call("server_throw", from, data[1], data[2])
		"prop_drop":       # id
			var d := _cand(int(data))
			if not d.is_empty() and is_instance_valid(d.body):
				d.body.call("server_release", from)

# ── Instantané (net_snapshot / net_apply_snapshot) ─────────────────────
# Entrées : ["b", id] objet cassé ; ["m", id, transform] objet déplacé (mesh hors de sa place d'origine).

## Serveur : état des objets qui ne sont plus tels qu'exportés.
func net_snapshot() -> Array:
	var out: Array = []
	for d in _bodies:
		if d.broken:
			out.append(["b", d.id])
		elif d.woken and is_instance_valid(d.mi):
			var t := (d.mi as MeshInstance3D).global_transform
			if not t.is_equal_approx(d.home):
				out.append(["m", d.id, t])
	return out

## Client qui arrive (ou serveur qui recharge son état) : objets cassés masqués, objets déplacés remis en place.
func net_apply_snapshot(entries: Array) -> void:
	for e in entries:
		if not (e is Array and e.size() >= 2 and e[0] is String and e[1] is int):
			continue
		var d := _cand(int(e[1]))
		if d.is_empty() or d.broken or not is_instance_valid(d.mi):
			continue
		if e[0] == "b":
			d.broken = true
			if _net == NET_SERVER:
				_srv_unsync(d)
			(d.mi as MeshInstance3D).visible = false
			if d.body != null and is_instance_valid(d.body):
				(d.body as Node).queue_free()
			d.body = null
		elif e[0] == "m" and e.size() == 3 and e[2] is Transform3D:
			d.woken = true                   # reste à sa nouvelle place (pas de retour « home » par la distance)
			(d.mi as MeshInstance3D).global_transform = e[2]
			if d.body != null and is_instance_valid(d.body):
				(d.body as Node3D).global_transform = Transform3D((e[2] as Transform3D).basis.orthonormalized(), e[2].origin)
			elif _net == NET_SERVER:
				_make_body(d)                # corps figé à la nouvelle place (collisions du serveur)

## Remise à zéro du monde par l'admin (serveur dédié, sans redémarrage) : chaque objet revient à sa place d'origine,
## intact, endormi ; corps libérés (recréés à la demande près de la caméra). Appelé chez le serveur et chez chaque
## client (level_sync.gd de CyberRealm, groupe net_snapshot).
func net_reset() -> void:
	for d in _bodies:
		if d.body != null and is_instance_valid(d.body):
			(d.body as Node).queue_free()
		d.body = null
		if _net == NET_SERVER:
			_srv_unsync(d)
		d.woken = false
		d.broken = false
		if is_instance_valid(d.mi):
			(d.mi as MeshInstance3D).visible = true
			(d.mi as MeshInstance3D).global_transform = d.home

func _on_peer_left(peer: int) -> void:
	for d in _bodies:
		if d.body != null and is_instance_valid(d.body):
			d.body.call("server_release", peer)

# ── Réseau : client ──────────────────────────────────────────────────

## Demande au serveur (client d'une session dédiée).
func net_send(topic: String, data: Variant = null, reliable := true) -> void:
	if _lan != null and _net == NET_CLIENT:
		_lan.send_level_message(topic, data, reliable)

func _cl_message(topic: String, data: Variant) -> void:
	match topic:
		"prop_break":      # [id, vel, position]
			if data is Array and data.size() == 3:
				var d := _cand(int(data[0]))
				if d.is_empty() or d.broken:
					return
				d.broken = true
				var mi := d.mi as MeshInstance3D
				PropBodyScript.spawn_shards(mi.get_parent(), mi, data[2], data[1])
				if is_instance_valid(d.body):
					(d.body as Node).queue_free()
				d.body = null
		"prop_grab_denied":
			var d := _cand(int(data))
			if not d.is_empty() and is_instance_valid(d.body):
				d.body.call("net_grab_denied")

# Les corps miroirs (cinématiques) suivent leur mesh, que la synchro du serveur déplace.
func _client_follow() -> void:
	for d in _bodies:
		var b = d.body
		if b == null or not is_instance_valid(b):
			continue
		var t := (d.mi as MeshInstance3D).global_transform
		if not t.origin.is_equal_approx((b as Node3D).global_position):
			(b as Node3D).global_transform = Transform3D(t.basis.orthonormalized(), t.origin)
			d.woken = true

func _process(delta: float) -> void:
	_t += delta
	if not _sway.is_empty() and _net != NET_SERVER:
		_update_sway()

func _physics_process(delta: float) -> void:
	if _net == NET_CLIENT:
		_client_follow()
	_acc += delta
	if _acc < 0.25:
		return
	_acc = 0.0
	_update_lights()
	_update_bodies()
