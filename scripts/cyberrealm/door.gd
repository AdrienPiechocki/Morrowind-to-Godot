extends StaticBody3D
## Porte cliquable : attaché par interiors.gd au collider du mesh de la porte
## (ou à une zone de repli créée à l'exécution). Le clic gauche appelle interact() sur TOUTES les machines de la
## session (world_interactor) ; interiors.gd ne téléporte que le joueur dont
## l'id correspond à celui qui a cliqué.

var manager: Node = null
var prompt := ""
var data := {}      # cible/sens de la porte (runtime, jamais sérialisé)

func interact(player_id: int) -> void:
	if manager != null and is_instance_valid(manager):
		manager.use_door(self, player_id)

func get_interact_prompt() -> String:
	return prompt
