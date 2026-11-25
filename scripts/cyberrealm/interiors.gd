extends Node3D
## Portes Morrowind : extérieur <-> intérieurs, solo et LAN.
##
## Les ~80 intérieurs sont des enfants de ce nœud, rangés sur une grille haute
## (y = 6000 m, au-dessus de tout le relief) et INACTIFS tant que personne n'y
## est : invisibles + process_mode DISABLED (leurs colliders quittent la
## physique). Le niveau reste ainsi un seul arbre sérialisable par le LevelBaker
## (les intérieurs voyagent dans le blob LAN avec le reste).
##
## Portes. Les marqueurs « Doors » (extérieur et intérieurs) portent
## target_scene / arrival_position / arrival_yaw. Ce sont les MESHES des portes
## (leur collider glTF) qui sont cliquables :
##   - entrée : le mesh de porte situé sur le marqueur (même id) ;
##   - sortie : dans l'intérieur visé, la porte la plus proche du point
##     d'arrivée (celle par laquelle on est entré), qui ramène devant le
##     marqueur d'origine.
## door.gd est attaché au collider de la porte ; ses données (cible, texte) sont
## des variables runtime, non sérialisées : chaque machine les repose au
## chargement. Sans mesh trouvé, repli sur une zone cylindrique invisible.
## Seul le joueur qui clique est téléporté, donc le relais LAN des interactions
## n'a pas besoin de retrouver le nœud chez les autres.
##
## Multijoueur. interact() est appelé sur TOUTES les machines ; seule celle du
## joueur qui a cliqué téléporte son joueur local. Tout l'état « où suis-je »
## (intérieur actif, pluie) est LOCAL. À l'intérieur, la caméra du joueur local
## reçoit une copie de l'environnement du niveau sans ciel ni brume (hors blob). Les autres joueurs voient simplement votre
## avatar se téléporter (sa position est déjà synchronisée par le LAN).

const DoorScript := preload("res://user/Levels/Morrowind/scripts/door.gd")
const PropsScript := preload("res://user/Levels/Morrowind/scripts/props.gd")

const INTERIOR_LAYER := 2                 # couche visuelle (bit 2) des intérieurs
const EXTERIOR_LAYER := 4                 # couche visuelle 3 : monde extérieur (masqué de la caméra dans un intérieur)
const SUN_MASK := 1 | EXTERIOR_LAYER      # le soleil éclaire l'extérieur et les avatars (couche 1), pas les intérieurs
const DOOR_PHYS_LAYER := 1 << 19          # couche physique dédiée (rayons seulement)
const INDOOR_MIN_Y := 3000.0              # au-dessus = dans un intérieur
const FALL_LIMIT := 60.0                  # chute sous le plancher haut -> retour dehors
const DEPTH_GRACE := 5               # marge sous un point d'arrivée immergé (m)
const FEET_TO_ORIGIN := 0.55              # origine du joueur au-dessus du sol (capsule)
const EXIT_BEHIND := 1.2                  # la porte de sortie est derrière le point d'arrivée

var _interiors := {}      # clé (nom de fichier) -> Node3D racine de l'intérieur
var _names := {}          # clé -> nom lisible
var _current := ""        # "" = dehors, sinon clé de l'intérieur actif (local)
var _busy := false
var _base_depth := -1     # MaxDepth du joueur tel que défini par le niveau (lu une fois)
var _entered := {}        # clé d'intérieur -> {target, marker} de la porte empruntée (local)
var _player: Node3D = null
var _weather: Node3D = null

func _ready() -> void:
	set_meta("lan_skip_children", true)
	add_to_group("waypoint_scope")
	var level := get_parent()
	if _is_dedicated_server():
		_server_ready(level)
		return
	_weather = level.get_node_or_null("Weather") as Node3D
	var sun := level.get_node_or_null("Sun") as Light3D
	if sun != null:
		sun.light_cull_mask = SUN_MASK
	# Monde extérieur sur sa propre couche visuelle : dans un intérieur, la caméra l'ignore (cull_mask). Sans
	# cela, les fissures entre les faces des meshes laissent voir le terrain, éclairé, 6 km plus bas.
	for nm in ["ExteriorRoot", "Farlands", "Water", "RedMountain", "Weather"]:
		var ext := level.get_node_or_null(nm)
		if ext != null:
			for vi in ext.find_children("*", "VisualInstance3D", true, false):
				(vi as VisualInstance3D).layers = EXTERIOR_LAYER
	for c in get_children():
		if c is Node3D:
			_interiors[_key(String(c.name))] = c
	# État de départ identique partout (le blob peut avoir été baké pendant que
	# l'hôte se trouvait dans un intérieur) : on active tout, on construit les
	# portes, puis on désactive tout.
	var ext_model := level.get_node_or_null("ExteriorRoot/Exterior")
	if ext_model != null:
		_fix_materials(ext_model)
		_fix_flora(ext_model)
	for key in _interiors:
		var it: Node3D = _interiors[key]
		it.process_mode = Node.PROCESS_MODE_INHERIT
		_prepare_interior(key, it)
	_build_doors(level)
	# Décor vivant (lanternes, balancement, objets physiques) : nœud runtime, ignoré du bake LAN (voir props.gd).
	var props := PropsScript.new()
	props.name = "Props"
	add_child(props)
	props.setup([ext_model] + _interiors.values())
	for key in _interiors:
		_deactivate(key)
	if _weather != null:
		_weather.visible = true

# Serveur dédié (headless, sans joueur ni rendu) : seul ce nœud garde son script (méta « net_server »). Il ne fait
# que préparer la physique du décor : les intérieurs restent ACTIFS (leurs colliders existent) et props.gd tourne en
# mode serveur — autorité sur les objets que les clients touchent, prennent ou lancent.
func _is_dedicated_server() -> bool:
	var lan := get_node_or_null("/root/WaylandRoom/LAN")
	return lan != null and lan.get("dedicated") == true

func _server_ready(level: Node) -> void:
	set_process(false)
	for c in get_children():
		if c is Node3D:
			_interiors[_key(String(c.name))] = c
			c.process_mode = Node.PROCESS_MODE_INHERIT
	var props := PropsScript.new()
	props.name = "Props"
	add_child(props)
	props.setup([level.get_node_or_null("ExteriorRoot/Exterior")] + _interiors.values())
	print("[server] décor physique prêt : %d intérieurs" % _interiors.size())

func _exit_tree() -> void:
	# Niveau remplacé (jonction LAN, retour au niveau local) : ne pas laisser
	# l'environnement intérieur sur la caméra du joueur.
	_apply_environment("")

func _prepare_interior(key: String, it: Node3D) -> void:
	for vi in it.find_children("*", "VisualInstance3D", true, false):
		(vi as VisualInstance3D).layers = INTERIOR_LAYER
		# Pas d'occlusion culling dans les intérieurs : les feuillages (alpha scissor) y disparaissaient
		# selon la position du joueur.
		if vi is GeometryInstance3D:
			(vi as GeometryInstance3D).ignore_occlusion_culling = true
			# Niveau de détail maximal en permanence : l'import glTF génère des LOD (simplification des meshes) qui
			# écrasent les tapis (éventail central), les tapisseries et autres petites pièces selon la distance, d'où
			# des textures qui « changent » quand on bouge.
			(vi as GeometryInstance3D).lod_bias = 100.0
	_fix_materials(it)
	_fix_floor_colliders(it)

# Flore extérieure : elle disparaissait au loin, pour les deux mêmes raisons que dans les intérieurs.
#  - occlusion culling : le tampon d'occlusion est basse résolution ; au loin, le pixel d'une plante posée sur le
#    terrain contient aussi le terrain devant elle (occludeur), la plante est jugée cachée ;
#  - LOD générés à l'import (generate_lods) : la simplification réduit à rien les quelques quads d'un feuillage.
# Les feuillages (nom de flore ou surface alpha scissor) ignorent donc l'occlusion et gardent leur mesh complet.
const FLORA_WORDS := ["flora", "tree_", "_tree", "grass", "bush", "shrub", "plant", "fern", "kelp", "weed", "reed", "leaf",
	"leaves", "vine", "mushroom", "shroom", "fungus", "lichen"]

func _fix_flora(root_n: Node) -> void:
	for mi in root_n.find_children("*", "MeshInstance3D", true, false):
		var g := mi as MeshInstance3D
		if g.mesh == null:
			continue
		if _has_word(String(g.name).to_lower(), FLORA_WORDS) or _has_scissor(g):
			g.ignore_occlusion_culling = true
			g.lod_bias = 100.0

static func _has_scissor(mi: MeshInstance3D) -> bool:
	for s in mi.mesh.get_surface_count():
		var m := mi.get_active_material(s) as BaseMaterial3D
		if m != null and m.transparency == BaseMaterial3D.TRANSPARENCY_ALPHA_SCISSOR:
			return true
	return false

# ── Corrections de géométrie (export Morrowind) ─────────────────────

# Éléments STRUCTURELS exportés sans collider (rochers/parois de grotte, boulders, murs et ponts des
# ruines, sols de fosse, plateformes…) : le joueur les traverse. On génère un collider pour les meshes
# dont le nom évoque de la structure ET qui font au moins MIN_STRUCT_SIZE mètres ; le décor (mousse,
# tapis, cordes, lianes, bannières…) en reste dépourvu.
const STRUCT_WORDS := ["rock", "boulder", "floor", "flr", "column", "wall", "bridge", "mezz", "plank",
	"pit", "beam", "ramp", "stair", "platform", "walkway", "deck", "pole", "ceil", "blacksquare"]
const NOT_STRUCT_WORDS := ["moss", "rug", "rope", "vine", "banner", "kelp", "_dj", "web", "curtain",
	"tapestry", "cloth", "torch", "scorch", "lamp", "light"]
const MIN_STRUCT_SIZE := 1.2
# Feuillage (alpha scissor) : avec les mipmaps, les feuilles s'amincissent puis DISPARAISSENT quand on
# s'éloigne. Seuil d'alpha abaissé + alpha-to-coverage (effectif avec le MSAA).
const ALPHA_CUTOFF := 0.28
var _fixed_mats := {}
var _colliders_done := {}   # clé d'intérieur -> true

const RUG_WORDS := ["rug", "carpet"]

# Tapis : reconnus au nom du mesh, ou à la texture d'une de ses surfaces (tx_rug_*, tr_rug_*, pc_furn_rug_*…)
# quand le nom ne le dit pas.
func _is_rug(mi: MeshInstance3D) -> bool:
	var low := String(mi.name).to_lower()
	for w in RUG_WORDS:
		if low.contains(w):
			return true
	for s in mi.mesh.get_surface_count():
		var m := mi.mesh.surface_get_material(s) as BaseMaterial3D
		var tex := m.albedo_texture if m != null else null
		if tex == null:
			continue
		var path := (tex.resource_path + " " + String(tex.resource_name)).to_lower()
		for w in RUG_WORDS:
			if path.contains(w):
				return true
	return false

# Matériaux opaques et alpha scissor en DOUBLE FACE (les alpha BLEND restent simple face).
# Entre les faces des meshes (jonctions en T de la géométrie d'origine), de fines fissures laissent voir ce qui est
# derrière — la skybox — sous forme de traits clairs. En double face, le regard tombe sur l'envers des faces voisines
# (sombre) au lieu du ciel. Les feuillages y gagnent d'être visibles des deux côtés ; alpha scissor : ALPHA_CUTOFF.
# EXCEPTION : les nappes à deux quads superposés dans le même plan, de normales opposées (toiles, paravents, tissus :
# un quad par face). En double face, les deux quads se disputent la profondeur et l'image scintille / s'inverse ;
# en simple face, chaque côté montre son propre quad. Ces surfaces reçoivent une copie du matériau en simple face.
func _fix_materials(root_n: Node) -> void:
	for mi in root_n.find_children("*", "MeshInstance3D", true, false):
		var mesh := (mi as MeshInstance3D).mesh
		if mesh == null:
			continue
		var is_rug := _is_rug(mi as MeshInstance3D)
		# Tapis, PARTOUT (extérieur compris, pas seulement les intérieurs) : niveau de détail maximal. Les LOD
		# générés à l'import glTF écrasent l'éventail central des tapis ronds/ovales selon la distance (texture qui
		# « change » quand on bouge). Réglé par instance, avant le filtre « mesh déjà traité » ci-dessous.
		if is_rug:
			(mi as MeshInstance3D).lod_bias = 100.0
		if _fixed_mats.has(mesh):
			continue
		_fixed_mats[mesh] = true
		for s in mesh.get_surface_count():
			var m := mesh.surface_get_material(s) as BaseMaterial3D
			# Tapis : sans mipmaps. Les tapis ronds ont un mappage « mandala » (chaque secteur de l'éventail central
			# plaque la texture entière) : au centre les UV convergent, les mipmaps y donnent un noir pâteux qui change
			# avec la position. Sans mipmaps, le mandala d'origine reste net.
			if is_rug and m != null:
				m.texture_filter = BaseMaterial3D.TEXTURE_FILTER_LINEAR
			# BLEND : tel qu'exporté (simple face) — les tapisseries sont des tissus à deux couches (face avant et
			# arrière), le double face superposerait l'envers de la couche arrière à l'image.
			# (l'import glTF de Godot traduit alphaMode BLEND en ALPHA_DEPTH_PRE_PASS)
			if m == null or m.transparency == BaseMaterial3D.TRANSPARENCY_ALPHA \
					or m.transparency == BaseMaterial3D.TRANSPARENCY_ALPHA_DEPTH_PRE_PASS:
				continue
			var scissor := m.transparency == BaseMaterial3D.TRANSPARENCY_ALPHA_SCISSOR
			if (scissor or mesh.surface_get_array_len(s) <= 24) and _has_coincident_layers(mesh, s):
				m = m.duplicate() as BaseMaterial3D            # copie : le matériau d'origine peut être partagé
				mesh.surface_set_material(s, m)
				m.cull_mode = BaseMaterial3D.CULL_BACK
			else:
				m.cull_mode = BaseMaterial3D.CULL_DISABLED
			if scissor:
				m.alpha_scissor_threshold = minf(m.alpha_scissor_threshold, ALPHA_CUTOFF)
				m.alpha_antialiasing_mode = BaseMaterial3D.ALPHA_ANTIALIASING_ALPHA_TO_COVERAGE

# Vrai si la surface est une NAPPE PLANE à deux faces : tous les sommets dans un même plan (à 2 mm près) et des
# triangles orientés dans les deux sens (un quad par face, superposés). Limité aux petites surfaces (décor).
func _has_coincident_layers(mesh: Mesh, s: int) -> bool:
	if mesh.surface_get_array_len(s) > 400:
		return false
	var arr := mesh.surface_get_arrays(s)
	var vs: PackedVector3Array = arr[Mesh.ARRAY_VERTEX]
	var idx: PackedInt32Array = arr[Mesh.ARRAY_INDEX] if arr[Mesh.ARRAY_INDEX] != null else PackedInt32Array()
	if idx.is_empty():
		idx = PackedInt32Array(range(vs.size()))
	var n_ref := Vector3.ZERO
	var has_pos := false
	var has_neg := false
	for t in range(0, idx.size() - 2, 3):
		var a := vs[idx[t]]
		var nrm := (vs[idx[t + 1]] - a).cross(vs[idx[t + 2]] - a)
		if nrm.length() < 1e-9:
			continue
		nrm = nrm.normalized()
		if n_ref == Vector3.ZERO:
			n_ref = nrm
		var d := nrm.dot(n_ref)
		if d > 0.98:
			has_pos = true
		elif d < -0.98:
			has_neg = true
		else:
			return false                      # une troisième orientation : pas une simple nappe
	if not (has_pos and has_neg):
		return false
	var p0 := vs[idx[0]]
	for v in vs:
		if absf((v - p0).dot(n_ref)) > 0.002:
			return false
	return true

# Colliders concaves solides des deux faces (normales inversées = sol traversé).
func _fix_floor_colliders(root_n: Node) -> void:
	for cs in root_n.find_children("*", "CollisionShape3D", true, false):
		var sh := (cs as CollisionShape3D).shape as ConcavePolygonShape3D
		if sh != null and not sh.backface_collision:
			sh.backface_collision = true

# Génère, à la première entrée dans un intérieur, les colliders manquants de sa structure.
func _make_missing_colliders(key: String) -> void:
	if _colliders_done.has(key):
		return
	_colliders_done[key] = true
	var model := (_interiors[key] as Node3D).get_node_or_null("Model")
	if model == null:
		return
	for mi in model.find_children("*", "MeshInstance3D", true, false):
		var m := mi as MeshInstance3D
		if m.mesh == null:
			continue
		var low := String(m.name).to_lower()
		if not _has_word(low, STRUCT_WORDS) or _has_word(low, NOT_STRUCT_WORDS):
			continue
		var size := m.get_aabb().size * m.global_transform.basis.get_scale().abs()
		if maxf(size.x, maxf(size.y, size.z)) < MIN_STRUCT_SIZE or _has_collider(m):
			continue
		m.set_meta("lan_skip_children", true)   # recréé par chaque machine (pas baké)
		m.create_trimesh_collision()
		for c in m.get_children():
			if c is StaticBody3D:
				for cs in c.get_children():
					var sh := (cs as CollisionShape3D).shape as ConcavePolygonShape3D if cs is CollisionShape3D else null
					if sh != null:
						sh.backface_collision = true

# Collider importé (« -colonly ») : frère juste après le mesh, au même endroit.
func _has_collider(mi: MeshInstance3D) -> bool:
	var par := mi.get_parent()
	for i in range(mi.get_index() + 1, mini(mi.get_index() + 3, par.get_child_count())):
		var sb := par.get_child(i) as StaticBody3D
		if sb != null and sb.global_position.distance_to(mi.global_position) < 0.05:
			return true
	return false

# ── Construction des portes ──────────────────────────────────────────

const DUPLICATE_RADIUS := 1.3
const EXIT_SEARCH_RADIUS := 3.0           # rayon de recherche du mesh de la porte de sortie
const ENTER_SEARCH_RADIUS := 0.5          # le mesh d'une porte est à l'origine de son marqueur
const DOOR_WORDS := ["door"]              # noms prioritaires pour les sorties
const EXIT_WORDS := ["grate", "hatch", "gate", "exit", "trap", "entrance"]
const NOT_DOORS := ["jamb", "frame", "doorway", "arch", "indoor", "outdoor"]   # décor autour de la porte, pas la porte
# Objets ramassables dont le nom contient « door » (livre « bk_doorsofthespirit »…) : jamais des portes.
const NOT_DOOR_PREFIXES := ["bk_", "t_bk_", "bookskill", "sc_", "t_sc_", "misc_", "t_misc_", "ingred_", "t_ingred_"]

var _body_mesh := {}      # collider de porte -> son MeshInstance3D
var _cands := {}          # clé de portée -> Array de {mesh, body, claimed}

func _build_doors(level: Node) -> void:
	var scopes: Array = []   # [clé de portée, nœud "Doors"]
	var ext := level.get_node_or_null("ExteriorRoot/Doors")
	if ext != null:
		scopes.append(["", ext])
	var keys := _interiors.keys()
	keys.sort()
	for key in keys:
		var d := (_interiors[key] as Node3D).get_node_or_null("Doors")
		if d != null:
			scopes.append([key, d])
	# Noms lisibles des intérieurs (méta target_name des portes qui y mènent).
	for s in scopes:
		for m in (s[1] as Node).get_children():
			var k := _target_key(m)
			if k != "" and not _names.has(k):
				_names[k] = String(m.get_meta("target_name", k.replace("_", " ")))
	# Passe 1 : entrées (le mesh de la porte est exactement sur son marqueur).
	# Passe 2 : sorties (porte la plus proche du point d'arrivée, parmi celles
	# que personne n'a déjà prises). Sans mesh trouvé : zone cylindrique de repli.
	var exits: Array = []
	var n := 0
	for s in scopes:
		var scope_key: String = s[0]
		var scope_name := "Outside" if scope_key == "" else String(_names.get(scope_key, scope_key))
		for m in (s[1] as Node).get_children():
			var tkey := _target_key(m)
			if tkey == "" or not (m is Node3D):
				continue
			var ap: Vector3 = m.get_meta("arrival_position", Vector3.ZERO)
			var yaw := float(m.get_meta("arrival_yaw", 0.0))
			var prompt := "Enter: %s" % String(_names.get(tkey, tkey))
			var data := {"kind": "enter", "target": tkey, "pos": ap, "yaw": yaw, "marker": m, "scope": scope_key}
			var door_id := String(m.get_meta("door_id", ""))
			# Export récent : le marqueur est le point d'arrivée DEVANT la porte, la porte
			# elle-même est à « door_position » (repère du nœud Doors). Ancien export : marqueur = porte.
			var door_pos: Vector3 = (m as Node3D).global_position
			if m.has_meta("door_position"):
				door_pos = (s[1] as Node3D).to_global(m.get_meta("door_position") as Vector3)
			var body := _claim_at(scope_key, door_pos, door_id, tkey)
			# Pas de mesh de porte : pas d'entrée (aucune zone de repli, on ne passe que par les portes).
			if body != null:
				_bind(body, prompt, data)
			exits.append([n, m, tkey, ap, yaw, scope_key, scope_name])
			n += 1
	for e in exits:
		var m: Node3D = e[1]
		var tkey: String = e[2]
		var ap: Vector3 = e[3]
		var yaw: float = e[4]
		var fwd := Vector3(-sin(yaw), 0, -cos(yaw))
		var behind := ap - fwd * EXIT_BEHIND
		var prompt := "Exit: %s" % String(e[6])
		var data := {"kind": "exit", "target": e[5], "marker": m, "alts": []}
		var spot := (_interiors[tkey] as Node3D).to_global(behind)
		# La porte réciproque (entrée de l'intérieur vers cette source) fait déjà office de sortie : sans ce
		# test, un mesh voisin (cadre d'entrée, etc.) serait pris pour une seconde sortie.
		if _has_reciprocal(tkey, spot, String(e[5])):
			continue
		var body := _claim_exit(tkey, spot)
		if body != null:
			_bind(body, prompt, data)
		else:
			var dup = _exit_duplicate(tkey, spot, String(e[5]))
			if dup is StaticBody3D:
				# Même porte physique que la sortie d'une autre source : une seule
				# zone cliquable ; au clic on revient par la source réellement utilisée.
				((dup as StaticBody3D).data["alts"] as Array).append({"target": e[5], "marker": m})
			# Sinon : la porte réciproque existe déjà (« true »), ou il n'y a pas de mesh de porte : rien
			# n'est créé (jamais de zone de repli).
	# Portes sans destination : elles s'ouvrent et se ferment au clic, comme dans le jeu.
	var swing_scopes: Array = [""]       # TOUS les intérieurs, même ceux sans nœud « Doors »
	swing_scopes.append_array(keys)
	for scope in swing_scopes:
		for c in _candidates(scope):
			if not c["claimed"] and _is_swing_door(c["mesh"] as MeshInstance3D):
				var sbody := _body_of(c)
				if sbody != null:
					c["claimed"] = true
					c["kind"] = "swing"
					_body_mesh[sbody] = c["mesh"]
					_bind(sbody, "", {"kind": "swing", "mesh": c["mesh"]})

# Vrai battant (pivot sur la charnière) : « door » sans être une trappe, grille, etc.
func _is_swing_door(mi: MeshInstance3D) -> bool:
	var nm := String(mi.name).to_lower()
	return _has_word(nm, DOOR_WORDS) and not _has_word(nm, EXIT_WORDS)

# ── Meshes de portes ─────────────────────────────────────────────────

# Portes candidates d'une portée : MeshInstance3D dont le nom évoque une porte,
# avec leur collider (le « -colonly » de l'import glTF, frère juste après).
func _candidates(scope: String) -> Array:
	if _cands.has(scope):
		return _cands[scope]
	var root_n: Node = null
	if scope == "":
		root_n = get_parent().get_node_or_null("ExteriorRoot/Exterior")
	else:
		root_n = (_interiors[scope] as Node3D).get_node_or_null("Model")
	var out: Array = []
	if root_n != null:
		for mi in root_n.find_children("*", "MeshInstance3D", true, false):
			var nm := String(mi.name).to_lower()
			if (_has_word(nm, DOOR_WORDS) or _has_word(nm, EXIT_WORDS)) and not _has_word(nm, NOT_DOORS) \
					and not _starts_with_any(nm, NOT_DOOR_PREFIXES):
				out.append({"mesh": mi, "body": null, "claimed": false, "kind": ""})
	_cands[scope] = out
	return out

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

func _body_of(c: Dictionary) -> StaticBody3D:
	if c["body"] != null:
		return c["body"]
	var mi := c["mesh"] as MeshInstance3D
	var par := mi.get_parent()
	var body: StaticBody3D = null
	for i in range(mi.get_index() + 1, mini(mi.get_index() + 3, par.get_child_count())):
		var sb := par.get_child(i) as StaticBody3D
		if sb != null and sb.global_position.distance_to(mi.global_position) < 0.05:
			body = sb
			break
	if body == null and mi.mesh != null:
		# Pas de collider importé : on en génère un (runtime, non sérialisé).
		mi.set_meta("lan_skip_children", true)
		mi.create_trimesh_collision()
		body = mi.get_child(mi.get_child_count() - 1) as StaticBody3D
	c["body"] = body
	return body

# Porte d'une entrée : mesh à l'origine du marqueur (de préférence du bon id).
func _claim_at(scope: String, pos: Vector3, door_id: String, dest: String) -> StaticBody3D:
	var best: Dictionary = {}
	var best_d := ENTER_SEARCH_RADIUS
	var id_l := door_id.to_lower()
	for c in _candidates(scope):
		if c["claimed"]:
			continue
		var d := (c["mesh"] as Node3D).global_position.distance_to(pos)
		if id_l != "" and String(c["mesh"].name).to_lower().begins_with(id_l):
			d -= 1.0   # le bon id l'emporte sur un voisin
		if d < best_d:
			best_d = d
			best = c
	return _take(best, "enter", dest)

# Porte de sortie : la plus proche du point d'arrivée, « door » avant les autres.
func _claim_exit(scope: String, pos: Vector3) -> StaticBody3D:
	var best: Dictionary = {}
	var best_score := INF
	for c in _candidates(scope):
		if c["claimed"]:
			continue
		var d := (c["mesh"] as Node3D).global_position.distance_to(pos)
		if d > EXIT_SEARCH_RADIUS:
			continue
		var tier := 0.0 if _has_word(String(c["mesh"].name).to_lower(), DOOR_WORDS) else 10.0
		if d + tier < best_score:
			best_score = d + tier
			best = c
	return _take(best, "exit")

func _take(c: Dictionary, kind := "enter", dest := "") -> StaticBody3D:
	if c.is_empty():
		return null
	var body := _body_of(c)
	if body != null:
		_body_mesh[body] = c["mesh"]
		c["claimed"] = true
		c["kind"] = kind
		c["dest"] = dest
	return body

func _has_reciprocal(scope: String, pos: Vector3, dest: String) -> bool:
	for c in _candidates(scope):
		if c["claimed"] and c["kind"] == "enter" and c["dest"] == dest \
				and (c["mesh"] as Node3D).global_position.distance_to(pos) < EXIT_SEARCH_RADIUS:
			return true
	return false

# Une porte déjà prise PAR UNE AUTRE SORTIE tout près du point : même porte
# physique atteinte depuis une autre source (un mesh = une seule destination).
func _exit_duplicate(scope: String, pos: Vector3, dest: String) -> Variant:
	for c in _candidates(scope):
		if not c["claimed"]:
			continue
		var d := (c["mesh"] as Node3D).global_position.distance_to(pos)
		if c["kind"] == "exit" and d < DUPLICATE_RADIUS:
			return c["body"]
		if c["kind"] == "enter" and c["dest"] == dest and d < EXIT_SEARCH_RADIUS:
			return true     # la porte réciproque existe déjà : rien à ajouter
	return null

# Branche une porte existante (collider du mesh) sur le système d'interaction.
func _bind(body: StaticBody3D, prompt: String, data: Dictionary) -> void:
	if not body.has_method("interact"):
		body.set_script(DoorScript)
	body.manager = self
	body.prompt = prompt
	body.data = data
	_add_pick_proxy(body)

# Zone de visée un peu plus grande que la porte. Une trappe est souvent logée dans le plafond (ou le sol) de
# la coque : le collider de la coque se trouve alors devant le sien et intercepte le rayon d'interaction.
# Le proxy est sur une couche que le joueur ne heurte pas (rayons seulement) ; il suit la porte quand elle pivote.
const PICK_MARGIN := 0.12

func _add_pick_proxy(body: StaticBody3D) -> void:
	var mi := _body_mesh.get(body) as MeshInstance3D
	if mi == null or mi.mesh == null or body.has_node("PickProxy"):
		return
	var box := mi.mesh.get_aabb().grow(PICK_MARGIN)
	var proxy := StaticBody3D.new()
	proxy.name = "PickProxy"
	proxy.set_script(DoorScript)
	proxy.forward = body
	proxy.collision_layer = DOOR_PHYS_LAYER
	proxy.collision_mask = 0
	var shape := BoxShape3D.new()
	shape.size = box.size
	var cs := CollisionShape3D.new()
	cs.shape = shape
	cs.position = box.get_center()
	proxy.add_child(cs)
	body.set_meta("lan_skip_children", true)   # jamais baké : recréé par chaque machine
	body.add_child(proxy)

func _target_key(marker: Node) -> String:
	var t := String(marker.get_meta("target_scene", ""))
	if t == "":
		return ""
	var k := _key(t.get_file().get_basename())
	return k if _interiors.has(k) else ""

# Les noms de nœuds n'admettent pas « . » (remplacé par « _ » à l'instanciation).
static func _key(s: String) -> String:
	return s.replace(".", "_")

# ── Utilisation d'une porte ──────────────────────────────────────────

func use_door(door: Node, player_id: int) -> void:
	if player_id != get_tree().get_multiplayer().get_unique_id() or _busy:
		return
	_busy = true
	await _travel(door.data as Dictionary)
	_busy = false

func _travel(d: Dictionary) -> void:
	var p := _local_player()
	if p == null:
		return
	var from := _current
	var src: Dictionary = d
	if d["kind"] == "exit":
		src = _exit_source(d, from)
	var dest := String(src["target"])
	# Active la destination AVANT de viser dedans : ses colliders doivent être
	# dans la physique pour la recherche du point de sortie.
	if dest != "":
		_activate(dest)
		await get_tree().physics_frame
		await get_tree().physics_frame
	if not is_instance_valid(p):
		return
	var pos: Vector3
	var yaw: float
	if d["kind"] == "enter":
		pos = _on_ground((_interiors[dest] as Node3D).to_global(d["pos"] as Vector3))
		yaw = float(d["yaw"])
		_entered[dest] = {"target": String(d["scope"]), "marker": d["marker"]}
	else:
		var spot := _marker_spot(src["marker"] as Node3D)
		pos = spot["pos"]
		yaw = spot["yaw"]
	_teleport(p, pos, yaw)
	if from != "" and from != dest:
		_deactivate(from)
	_set_scope(dest)

# Waypoints (scripts/main/waypoints.gd) : téléportation à une position quelconque, intérieur compris.
# On active l'intérieur visé (colliders prêts) avant d'y poser le joueur, et on désactive l'ancien.
func waypoint_teleport(pos: Vector3, yaw: float) -> void:
	var p := _local_player()
	if p == null or _busy:
		return
	var from := _current
	var dest := _scope_at(pos)
	if dest != "":
		_activate(dest)
		if dest != from:
			await get_tree().physics_frame
			await get_tree().physics_frame
		if not is_instance_valid(p):
			return
	_teleport(p, pos, yaw)
	if from != "" and from != dest:
		_deactivate(from)
	_set_scope(dest)

# Source de retour d'une sortie : si cette porte est partagée par plusieurs
# sources (alts), on revient par celle qu'on a réellement empruntée.
func _exit_source(d: Dictionary, from: String) -> Dictionary:
	var alts: Array = d.get("alts", [])
	var used = _entered.get(from)
	if used != null and not alts.is_empty():
		if used["marker"] == d["marker"]:
			return d
		for a in alts:
			if a["marker"] == used["marker"]:
				return a
	return d

func _local_player() -> Node3D:
	if _player != null and is_instance_valid(_player):
		return _player
	_player = get_parent().find_child("Player", true, false) as Node3D
	if _player != null and "MaxDepth" in _player and _base_depth < 0:
		_base_depth = int(_player.get("MaxDepth"))
	return _player

func _teleport(p: Node3D, pos: Vector3, yaw: float) -> void:
	_adapt_max_depth(p, pos.y)
	p.global_position = pos
	p.rotation.y = yaw
	if "velocity" in p:
		p.set("velocity", Vector3.ZERO)
	var cam := p.get_node_or_null("Camera3D") as Camera3D
	if cam != null:
		cam.rotation.x = 0.0

# MaxDepth (respawn sous l'eau) : certaines portes extérieures (grottes, égouts,
# navires) débouchent SOUS le niveau de l'eau. Tant que le joueur y est, le seuil
# est abaissé sous son point d'arrivée ; il revient à la valeur du niveau dès
# qu'il remonte. Les intérieurs (y = 6000) ne sont jamais concernés.
func _adapt_max_depth(p: Node3D, y: float) -> void:
	if _base_depth < 0 or not ("MaxDepth" in p):
		return
	if y < INDOOR_MIN_Y and y <= -float(_base_depth):
		p.set("MaxDepth", int(ceil(-y)) + DEPTH_GRACE)
	else:
		p.set("MaxDepth", _base_depth)

# ── Portée locale (dehors / intérieur actif) ────────────────────────

func _set_scope(key: String) -> void:
	_current = key
	_apply_environment(key)
	if _weather != null and is_instance_valid(_weather):
		_weather.visible = key == ""

# Environnement des intérieurs : celui du niveau (mêmes effets : SSAO, glow,
# ajustements…) mais SANS ciel ni brume. Copie locale posée sur la caméra du
# joueur (jamais sérialisée) ; les réglages du menu graphique qui changent
# l'environnement du niveau y sont recopiés en cours de route.
const INTERIOR_SYNC_KEYS := ["ssr_enabled", "ssao_enabled", "ssil_enabled", "sdfgi_enabled",
	"glow_enabled", "glow_intensity", "glow_bloom", "glow_hdr_threshold", "glow_strength",
	"adjustment_enabled", "adjustment_brightness", "adjustment_contrast", "adjustment_saturation",
	"tonemap_mode", "tonemap_exposure", "tonemap_white"]
const INTERIOR_AMBIENT := Color(0.75, 0.78, 0.85)
const INTERIOR_AMBIENT_ENERGY := 0.5

var _interior_env: Environment = null
var _env_sync := 0.0

func _level_environment() -> Environment:
	var we := get_parent().get_node_or_null("WorldEnvironment") as WorldEnvironment
	return we.environment if we != null else null

func _build_interior_env() -> Environment:
	var src := _level_environment()
	var e: Environment = src.duplicate() if src != null else Environment.new()
	e.background_mode = Environment.BG_COLOR        # pas de ciel
	e.background_color = Color(0.02, 0.02, 0.03)
	e.sky = null
	e.fog_enabled = false                           # pas de brume
	e.volumetric_fog_enabled = false
	e.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR
	e.ambient_light_color = INTERIOR_AMBIENT
	e.ambient_light_energy = INTERIOR_AMBIENT_ENERGY
	return e

func _sync_interior_env() -> void:
	var src := _level_environment()
	if src == null or _interior_env == null:
		return
	for k in INTERIOR_SYNC_KEYS:
		var v = src.get(k)
		if v != null and _interior_env.get(k) != v:
			_interior_env.set(k, v)

func _apply_environment(key: String) -> void:
	var p := _player
	if p == null or not is_instance_valid(p):
		return
	var cam := p.get_node_or_null("Camera3D") as Camera3D
	if cam == null:
		return
	if key == "":
		cam.environment = null                      # WorldEnvironment du niveau
		cam.cull_mask = 0xFFFFF                     # tout est visible
		return
	cam.cull_mask = 0xFFFFF & ~EXTERIOR_LAYER      # intérieur : le monde extérieur est masqué
	if _interior_env == null:
		_interior_env = _build_interior_env()
	_sync_interior_env()
	cam.environment = _interior_env

func _activate(key: String) -> void:
	var it := _interiors.get(key) as Node3D
	if it != null:
		it.process_mode = Node.PROCESS_MODE_INHERIT
		it.visible = true
		_make_missing_colliders(key)

func _deactivate(key: String) -> void:
	var it := _interiors.get(key) as Node3D
	if it != null:
		it.visible = false
		it.process_mode = Node.PROCESS_MODE_DISABLED

# Fenêtre saisie (menu fenêtres) alors qu'elle est dans une AUTRE scène que le
# joueur (dehors / un autre intérieur) : le grab fige la distance caméra-fenêtre
# au moment de la prise, soit des milliers de mètres, et la fenêtre part au loin
# et disparaît. On la ramène donc devant le joueur, à distance de main.
const BRING_DEPTH := 2.5
var _grab_seen := -1

func _scope_at(pos: Vector3) -> String:
	if pos.y < INDOOR_MIN_Y:
		return ""
	var best := ""
	var best_d := INF
	for key in _interiors:
		var p := (_interiors[key] as Node3D).global_position
		var d := Vector2(pos.x - p.x, pos.z - p.z).length()
		if d < best_d:
			best_d = d
			best = key
	return best

func _bring_grabbed_window() -> void:
	var room := get_parent().get_parent()
	var w3 = room.get("win3d") if room != null else null
	if w3 == null or not bool(w3.get("is_moving")):
		_grab_seen = -1
		return
	var wid := int(w3.get("active_window_id"))
	if wid == _grab_seen:
		return
	_grab_seen = wid
	var quads = w3.get("quads")
	var quad := (quads as Dictionary).get(wid) as Node3D if quads is Dictionary else null
	var cam := get_viewport().get_camera_3d()
	if quad == null or not is_instance_valid(quad) or cam == null:
		return
	if _scope_at(quad.global_position) == _current:
		return
	w3.set("move_depth", BRING_DEPTH)
	quad.global_position = cam.global_position - cam.global_basis.z * BRING_DEPTH

# Fenêtre tenue (GRAB depuis le menu fenêtres) : elle flotte pile au viseur et
# son collider (couche 2) intercepte le rayon d'interaction du jeu, qui ne voit
# alors jamais la porte visée. Tant qu'une fenêtre est tenue, un clic gauche
# cherche donc la porte avec un rayon qui ignore les fenêtres ; la fenêtre suit
# ensuite le joueur dans la nouvelle scène.
const DOOR_REACH := 3.0

func _click_through_grabbed_window() -> void:
	if _busy or Input.mouse_mode != Input.MOUSE_MODE_CAPTURED \
			or not Input.is_action_just_pressed("left_click", false):
		return
	var room := get_parent().get_parent()
	var w3 = room.get("win3d") if room != null else null
	if w3 == null or int(w3.call("get_grabbed_window_id")) == -1:
		return
	var cam := get_viewport().get_camera_3d()
	if cam == null:
		return
	var from := cam.global_position
	var rq := PhysicsRayQueryParameters3D.create(from, from - cam.global_basis.z * DOOR_REACH, 1 | DOOR_PHYS_LAYER)
	var hit := get_world_3d().direct_space_state.intersect_ray(rq)
	if hit.is_empty():
		return
	var body := hit.collider as Node
	if body != null and body.has_method("interact") and "data" in body:
		body.call("interact", get_tree().get_multiplayer().get_unique_id())

# Filet de sécurité : respawn / chute hors d'un intérieur sans passer par une
# porte (ex. le joueur est ramené au spawn extérieur par player.gd).
func _process(_delta: float) -> void:
	_bring_grabbed_window()
	_click_through_grabbed_window()
	if _busy:
		return
	var p := _local_player()
	if p == null:
		return
	var y := p.global_position.y
	if _current != "":
		_env_sync -= _delta
		if _env_sync <= 0.0:
			_env_sync = 0.5
			_sync_interior_env()
	if _base_depth >= 0 and y > -float(_base_depth) + 0.5 and int(p.get("MaxDepth")) != _base_depth:
		p.set("MaxDepth", _base_depth)   # remonté au-dessus de l'eau
	if _current != "" and y < INDOOR_MIN_Y:
		_deactivate(_current)
		_set_scope("")
	elif _current != "" and y < _interior_floor_y() - FALL_LIMIT:
		var sp = p.get("spawn_pos")
		_deactivate(_current)
		_teleport(p, sp if sp is Vector3 else Vector3(0, 60, 0), p.rotation.y)
		_set_scope("")

func _interior_floor_y() -> float:
	for key in _interiors:
		return (_interiors[key] as Node3D).global_position.y
	return INDOOR_MIN_Y

# ── Point de sortie devant une porte ─────────────────────────────────

const GROUND_UP := 0.8      # m : départ du rayon de sol au-dessus du point (marche, seuil légèrement plus haut)
const GROUND_DOWN := 6.0    # m : profondeur de recherche du sol

## Point d'arrivée posé au sol. Les positions d'arrivée de Morrowind flottent souvent au-dessus du plancher (le jeu
## laisse tomber le joueur) : sans ce recalage on arrivait trop haut. Sans sol trouvé : position telle quelle.
func _on_ground(p: Vector3) -> Vector3:
	var space := get_world_3d().direct_space_state
	var rq := PhysicsRayQueryParameters3D.create(p + Vector3.UP * GROUND_UP, p + Vector3.DOWN * GROUND_DOWN, 1)
	var hit := space.intersect_ray(rq)
	if hit.is_empty() or (hit.normal as Vector3).y <= 0.5:
		return p + Vector3.UP * FEET_TO_ORIGIN
	return (hit.position as Vector3) + Vector3.UP * FEET_TO_ORIGIN

## Point d'arrivée devant une porte : le marqueur lui-même (position + orientation
## vers l'extérieur de la porte), posé au sol. Si le marqueur est dans un mur ou
## sans sol, on retombe sur la recherche autour de la porte.
func _marker_spot(marker: Node3D) -> Dictionary:
	var space := get_world_3d().direct_space_state
	var p := marker.global_position
	var yaw := marker.global_transform.basis.get_euler().y
	var rq := PhysicsRayQueryParameters3D.create(p + Vector3.UP * GROUND_UP, p + Vector3.DOWN * GROUND_DOWN, 1)
	var hit := space.intersect_ray(rq)
	if not hit.is_empty() and (hit.normal as Vector3).y > 0.5:
		var feet: Vector3 = hit.position
		var sq := PhysicsShapeQueryParameters3D.new()
		sq.shape = CapsuleShape3D.new()
		sq.collision_mask = 1
		sq.transform = Transform3D(Basis.IDENTITY, feet + Vector3.UP * (FEET_TO_ORIGIN + 0.5))
		if space.intersect_shape(sq, 1).is_empty():
			return {"pos": feet + Vector3.UP * FEET_TO_ORIGIN, "yaw": yaw}
	return _find_exit_spot(p)

## Cherche autour de `door_pos` (monde) un point libre pour la capsule du
## joueur, en vue directe de la porte : sol par rayon, capsule sans collision.
## La direction retenue est celle qui s'éloigne de la porte.
func _find_exit_spot(door_pos: Vector3) -> Dictionary:
	var space := get_world_3d().direct_space_state
	var capsule := CapsuleShape3D.new()   # même dimensions que le joueur
	for r in [1.6, 2.2, 1.1, 2.8]:
		for i in 16:
			var ang := TAU * float(i) / 16.0
			var dir := Vector3(sin(ang), 0, cos(ang))
			var p: Vector3 = door_pos + dir * r
			var rq := PhysicsRayQueryParameters3D.create(p + Vector3.UP * 1.2, p + Vector3.DOWN * 4.0, 1)
			var hit := space.intersect_ray(rq)
			if hit.is_empty():
				continue
			var feet: Vector3 = hit.position
			var sq := PhysicsShapeQueryParameters3D.new()
			sq.shape = capsule
			sq.collision_mask = 1
			sq.transform = Transform3D(Basis.IDENTITY, feet + Vector3.UP * (FEET_TO_ORIGIN + 0.5))
			if not space.intersect_shape(sq, 1).is_empty():
				continue
			var los := PhysicsRayQueryParameters3D.create(door_pos + Vector3.UP * 1.0, feet + Vector3.UP * 1.0, 1)
			if not space.intersect_ray(los).is_empty():
				continue
			return {"pos": feet + Vector3.UP * FEET_TO_ORIGIN, "yaw": atan2(-dir.x, -dir.z)}
	return {"pos": door_pos + Vector3(0, 1.0, 1.5), "yaw": 0.0}
