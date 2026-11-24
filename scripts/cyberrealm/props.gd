extends Node
## Décor vivant du niveau Morrowind, ajouté À L'EXÉCUTION (rien n'est écrit dans les .gltf) :
##   - lanternes / lampes : lumière chaude + halo émissif (extérieur : seulement quand le soleil est bas) ;
##   - lanternes suspendues : léger balancement (pendule autour du point d'accroche) ;
##   - petits objets sans collision (bouteilles, bols, livres…) : RigidBody3D que le joueur peut pousser.
## Les objets physiques se prennent / lancent / cassent (prop_body.gd).
## Les nœuds créés n'ont pas d'owner et leur parent porte « lan_skip_children » : le LevelBaker les ignore,
## chaque machine les reconstruit au chargement (comme les portes). CYBERREALM_NO_PROPS=1 désactive tout.

const PropBodyScript := preload("res://user/Levels/Morrowind/scripts/prop_body.gd")

const PROP_LAYER := 1 << 20                # couche physique des objets de décor (voir player.gd)

const GLOW_WORDS := ["lantern", "lightpost", "lamp", "torch", "sconce", "chandelier"]
const NO_HALO_WORDS := ["lightpost", "torch"]            # lumière seule, pas de halo sur le mesh
const SWAY_WORDS := ["lantern"]                                     # suspendues
# Tout objet SANS collider devient physique, sauf la flore, la structure et le décor souple/lumineux.
const NOT_PHYS_WORDS := ["-colonly", "plant", "flora", "grass", "tree_", "_tree", "bush", "shrub", "leaf", "leaves",
	"fern", "flower", "kelp", "weed", "reed", "moss", "mushroom", "fungus", "vine", "root", "stalk", "bloom", "petal",
	"lichen", "cactus", "coral", "bramble", "heather", "wickwheat", "terrain", "land_",
	"wall", "floor", "flr", "door", "stair", "ceil", "beam", "arch_", "archway", "pillar", "column", "bridge",
	"rock", "boulder", "rug", "rope", "banner", "curtain", "tapestry", "web", "waterfall", "watersurface", "fire_",
	"fireplace", "smoke", "steam", "lantern", "lamp", "torch", "signpost", "signbase", "active_sign", "post",
	"rack", "hook", "coals", "practice_mat"]
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
const FADE_BEGIN := 22.0                  # m : la lumière s'éteint progressivement (perf iGPU)
const FADE_LENGTH := 10.0
const INDOOR_MIN_Y := 3000.0

var _lights: Array = []        # {light, base, outdoor}
var _sway: Array = []          # {mi, base, pivot, gpos, amp, freq, phase}
var _statics: Array = []       # {mi, gpos, body}
var _bodies: Array = []        # {mi, gpos, home, body (null si non instancié), woken}
var _glow_mats := {}           # (matériau source, extérieur) -> copie émissive partagée
var _glow_list: Array = []     # {mat, outdoor}
var _t := 0.0
var _acc := 0.0
var _sun: Light3D
var _mi_seen := {}

func setup(roots: Array) -> void:
	if OS.get_environment("CYBERREALM_NO_PROPS") == "1":
		return
	_sun = get_parent().get_parent().get_node_or_null("Sun") as Light3D
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
	var par := mi.get_parent()
	for i in range(mi.get_index() + 1, mini(mi.get_index() + 3, par.get_child_count())):
		var sb := par.get_child(i) as StaticBody3D
		if sb != null and sb.global_position.distance_to(mi.global_position) < 0.05:
			return true
	return false

func _process_mesh(mi: MeshInstance3D) -> void:
	if mi.mesh == null or _mi_seen.has(mi):
		return
	_mi_seen[mi] = true
	var low := String(mi.name).to_lower()
	if _has_word(low, GLOW_WORDS) and not low.contains("-colonly"):
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
	light.distance_fade_enabled = true
	light.distance_fade_begin = FADE_BEGIN
	light.distance_fade_length = FADE_LENGTH
	light.light_cull_mask = mi.layers | 1       # couche du mesh (extérieur / intérieur) + avatars
	light.position = mi.get_aabb().get_center()
	mi.set_meta("lan_skip_children", true)
	mi.add_child(light)
	var outdoor := mi.global_position.y < INDOOR_MIN_Y
	if not _has_word(low, NO_HALO_WORDS):
		_make_glow(mi, outdoor)
	_lights.append({"light": light, "base": LIGHT_ENERGY, "outdoor": outdoor})

# La lanterne elle-même s'illumine : copie des matériaux du mesh avec émission = sa propre texture (les
# émissions d'origine sont à 0 dans l'export). Une copie par matériau source, partagée entre les instances.
func _make_glow(mi: MeshInstance3D, outdoor: bool) -> void:
	for i in mi.mesh.get_surface_count():
		if mi.get_surface_override_material(i) != null:
			continue                                  # déjà traité (niveau reçu en LAN, déjà baké)
		var src := mi.mesh.surface_get_material(i) as StandardMaterial3D
		if src == null:
			continue
		var key := [src, outdoor]
		if not _glow_mats.has(key):
			var m := src.duplicate() as StandardMaterial3D
			m.emission_enabled = true
			m.emission = Color(1.0, 0.62, 0.3)
			m.emission_energy_multiplier = 0.12
			if m.albedo_texture != null:
				m.emission_texture = m.albedo_texture
				m.emission = Color(1.0, 0.8, 0.55)
			m.emission_operator = BaseMaterial3D.EMISSION_OP_ADD
			_glow_mats[key] = m
			_glow_list.append({"mat": m, "outdoor": outdoor})
		mi.set_surface_override_material(i, _glow_mats[key])

# Extérieur : allumées quand le soleil/la lune est faible. Appelé quelques fois par seconde.
func _update_lights() -> void:
	var dark := 1.0
	if _sun != null:
		dark = 1.0 - smoothstep(0.1, 0.9, _sun.light_energy)
	for g in _glow_list:
		if g.outdoor:
			(g.mat as StandardMaterial3D).emission_energy_multiplier = lerpf(0.01, 0.12, dark)
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
	_bodies.append({"mi": mi, "gpos": mi.global_position, "home": mi.global_transform, "body": null, "woken": false})

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

func _update_statics(cp: Vector3) -> void:
	var near2 := STATIC_NEAR * STATIC_NEAR
	var far2 := STATIC_FAR * STATIC_FAR
	for d in _statics:
		var sb = d.body
		if sb != null and not is_instance_valid(sb):
			d.body = null
			sb = null
		var d2 := (d.gpos as Vector3).distance_squared_to(cp)
		if sb == null:
			if d2 < near2 and is_instance_valid(d.mi) and (d.mi as MeshInstance3D).is_visible_in_tree():
				_make_static(d)
		elif d2 > far2:
			sb.queue_free()
			d.body = null

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
	body.name = "PropBody"
	body.mass = clampf(size.x * size.y * size.z * 600.0, 0.1, 4.0)
	body.collision_layer = PROP_LAYER            # couche à part : le joueur ne subit jamais leur mouvement (player.gd)
	body.collision_mask = 1 | PROP_LAYER
	body.freeze = true
	body.freeze_mode = RigidBody3D.FREEZE_MODE_STATIC
	body.linear_damp = 0.4
	body.angular_damp = 1.5
	var cs := CollisionShape3D.new()
	cs.shape = shape
	cs.position = aabb.get_center() * sc
	body.add_child(cs)
	var par := mi.get_parent()
	par.set_meta("lan_skip_children", true)
	par.add_child(body)
	body.global_transform = Transform3D(mi.global_transform.basis.orthonormalized(), mi.global_position)
	# Le mesh d'origine reste en place dans l'arbre (donc baké normalement) et suit le corps.
	var rt := RemoteTransform3D.new()
	rt.update_scale = false
	body.add_child(rt)
	rt.remote_path = rt.get_path_to(mi)
	d.body = body

func _update_bodies() -> void:
	var cam := get_viewport().get_camera_3d()
	if cam == null:
		return
	var cp := cam.global_position
	_update_statics(cp)
	var near2 := BODY_NEAR * BODY_NEAR
	var far2 := BODY_FAR * BODY_FAR
	for d in _bodies:
		var body = d.body
		if body != null and not is_instance_valid(body):
			d.body = null
			continue
		if body != null:
			var rb := body as RigidBody3D
			if not rb.freeze:
				d.woken = true                       # touché / pris : il reste, indépendant de la distance
				if rb.global_position.y < (d.home as Transform3D).origin.y - 15.0:    # traversé le décor
					rb.freeze = true
					rb.global_transform = d.home
					d.woken = false
			elif not d.woken and (d.gpos as Vector3).distance_squared_to(cp) > far2:
				(d.mi as MeshInstance3D).global_transform = d.home
				rb.queue_free()
				d.body = null
		elif not d.woken and (d.gpos as Vector3).distance_squared_to(cp) < near2 \
				and is_instance_valid(d.mi) and (d.mi as MeshInstance3D).is_visible_in_tree():
			_make_body(d)

func _process(delta: float) -> void:
	_t += delta
	if not _sway.is_empty():
		_update_sway()

func _physics_process(delta: float) -> void:
	_acc += delta
	if _acc < 0.25:
		return
	_acc = 0.0
	_update_lights()
	_update_bodies()
