extends Node3D
# Farlands : regroupe les copies d'un meme modele en MultiMesh (un draw call par materiau au lieu d'un par
# copie), coupe les ombres (la distance d'ombre est de toute facon courte) et masque les chunks que la brume
# cache deja. Les transformations viennent de lod_<cx>_<cy>.inst.json (a cote du .gltf de chaque chunk).

@export var cast_shadows := false
@export var visibility_margin_m := 900.0   # chunks masques au-dela de DayNight.view_distance + cette marge (0 = jamais)
@export var daynight: Node

func _ready() -> void:
	if daynight == null:
		daynight = get_parent().get_node_or_null("DayNight")
	var view_end := 0.0
	if daynight and visibility_margin_m > 0.0:
		var vd: float = daynight.get("view_distance")
		if vd > 0.0:
			view_end = vd + visibility_margin_m
	for chunk in get_children():
		_instance_chunk(chunk)
		for gi in chunk.find_children("*", "GeometryInstance3D", true, false):
			var g := gi as GeometryInstance3D
			if not cast_shadows:
				g.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
			if view_end > 0.0:
				g.visibility_range_end = view_end

func _instance_chunk(chunk: Node) -> void:
	var path: String = chunk.scene_file_path
	if path == "":
		return
	var jp := path.get_basename() + ".inst.json"
	if not FileAccess.file_exists(jp):
		return
	var data = JSON.parse_string(FileAccess.get_file_as_string(jp))
	if not (data is Dictionary):
		return
	for nm in data:
		var mi := chunk.find_child(nm, true, false) as MeshInstance3D
		if mi == null or mi.mesh == null:
			continue
		var arr: Array = data[nm]
		var n := arr.size() / 8                  # tx ty tz qx qy qz qw s
		var mm := MultiMesh.new()
		mm.transform_format = MultiMesh.TRANSFORM_3D
		mm.mesh = mi.mesh
		mm.instance_count = n
		for i in n:
			var o := i * 8
			var b := Basis(Quaternion(arr[o + 3], arr[o + 4], arr[o + 5], arr[o + 6]))
			mm.set_instance_transform(i, Transform3D(b.scaled(Vector3.ONE * float(arr[o + 7])),
					Vector3(arr[o], arr[o + 1], arr[o + 2])))
		var mmi := MultiMeshInstance3D.new()
		mmi.name = nm + "_multi"
		mmi.multimesh = mm
		mi.get_parent().add_child(mmi)
		mmi.global_transform = mi.global_transform
		mi.queue_free()
