# src/services/import_service.py

import os
import json
from pathlib import Path
from typing import List, Dict, Optional, Union

from src.models import Dataset, Image, Annotation, BoundingBox
from src.models.enums import ImageSource, AnnotationType, DatasetFormat
from src.services.api_service import APIService
from src.utils.logger import Logger
from src.core.exceptions import ImportError, ValidationError

class ImportService:
    """Service d'importation de datasets"""
    
    def __init__(
        self, 
        api_service: Optional[APIService] = None,
        logger: Optional[Logger] = None
    ):
        """
        Initialise le service d'import
        
        Args:
            api_service: Service API pour les imports distants
            logger: Gestionnaire de logs
        """
        self.api_service = api_service or APIService()
        self.logger = logger or Logger()
    


    def import_from_mapillary(
        self, 
        dataset: Dataset, 
        bbox: Dict[str, float], 
        max_images: int = 100
    ) -> Dataset:
        """
        Importe des images depuis Mapillary avec traitement amélioré des annotations.
        
        Args:
            dataset: Dataset de destination
            bbox: Bounding box géographique
            max_images: Nombre maximum d'images à importer
                
        Returns:
            Dataset mis à jour
        """
        try:
            # Vérifier que le dataset n'est pas None
            if dataset is None:
                self.logger.error("Le dataset fourni est None")
                raise ImportError("Dataset invalide pour l'import")
                
            # Récupérer les images de la zone - MODIFIÉ: filtrer uniquement les images contenant des panneaux
            self.logger.info(f"Récupération d'images depuis Mapillary dans la zone: {bbox}")
            images = self.api_service.get_images_in_bbox(
                bbox, 
                limit=max_images, 
                force_refresh=True,
                # Filtrer uniquement les images contenant des panneaux
                object_types=["regulatory", "warning", "information", "complementary"]
            )
            
            if not images:
                self.logger.warning("Aucune image trouvée dans la zone spécifiée avec des panneaux")
                raise ImportError("Aucune image trouvée dans la zone spécifiée avec des panneaux")
            
            self.logger.info(f"Récupération de {len(images)} images depuis Mapillary")
            
            # Télécharger les annotations et les images avec barre de progression
            total_annotations = 0
            images_with_annotations = 0
            
            for i, image in enumerate(images):
                self.logger.debug(f"Traitement de l'image {i+1}/{len(images)}: {image.id}")
                
                # Récupérer les détections pour chaque image - forcer le rafraîchissement
                try:
                    annotations = self.api_service.get_image_detections(image.id, force_refresh=True)
                    
                    if annotations:
                        self.logger.info(f"Récupération de {len(annotations)} annotations pour l'image {image.id}")
                        total_annotations += len(annotations)
                        images_with_annotations += 1
                        
                        # S'assurer que les annotations sont valides avant de les ajouter
                        valid_annotations = []
                        for annotation in annotations:
                            # Vérifier que les coordonnées sont dans les limites (0-1)
                            if (0 <= annotation.bbox.x <= 1 and 
                                0 <= annotation.bbox.y <= 1 and
                                0 < annotation.bbox.width <= 1 and 
                                0 < annotation.bbox.height <= 1 and
                                annotation.bbox.x + annotation.bbox.width <= 1 and
                                annotation.bbox.y + annotation.bbox.height <= 1):
                                valid_annotations.append(annotation)
                            else:
                                self.logger.warning(
                                    f"Annotation ignorée pour l'image {image.id} - coordonnées hors limites: "
                                    f"x={annotation.bbox.x}, y={annotation.bbox.y}, "
                                    f"width={annotation.bbox.width}, height={annotation.bbox.height}"
                                )
                        
                        # Si aucune annotation valide, investiguer pourquoi
                        if not valid_annotations and annotations:
                            self.logger.warning(f"Image {image.id}: {len(annotations)} annotations récupérées mais aucune valide")
                        
                        # Ajouter les annotations valides à l'image
                        for annotation in valid_annotations:
                            image.add_annotation(annotation)
                            
                        self.logger.info(f"Ajout de {len(valid_annotations)} annotations valides à l'image {image.id}")
                    else:
                        self.logger.warning(f"Aucune annotation trouvée pour l'image {image.id}")
                    
                except Exception as e:
                    self.logger.warning(f"Impossible de récupérer les annotations pour {image.id}: {str(e)}")
                    import traceback
                    self.logger.warning(traceback.format_exc())
                
                # Télécharger l'image
                try:
                    # Vérifier que le chemin d'image est valide
                    if not hasattr(image, 'path') or not image.path:
                        self.logger.warning(f"Chemin d'image invalide pour {image.id}")
                        continue
                        
                    # S'assurer que l'URL a un préfixe https:// si nécessaire
                    image_path = str(image.path)
                    if image_path and not image_path.startswith(('http://', 'https://')):
                        image_path = f"https://{image_path}"
                        
                    self.logger.debug(f"Téléchargement de l'image depuis: {image_path}")
                    image_data = self.api_service.download_image(image_path)
                    
                    if image_data:
                        # Sauvegarder l'image localement
                        local_path = dataset.path / "images"
                        local_path.mkdir(parents=True, exist_ok=True)
                        
                        file_path = local_path / f"{image.id}.jpg"
                        with open(file_path, 'wb') as f:
                            f.write(image_data)
                        
                        # Mettre à jour le chemin de l'image
                        image.path = file_path
                        self.logger.debug(f"Image sauvegardée localement: {file_path}")
                    else:
                        self.logger.warning(f"Échec du téléchargement de l'image {image.id}")
                except Exception as e:
                    self.logger.warning(f"Impossible de télécharger l'image {image.id}: {str(e)}")
                    import traceback
                    self.logger.warning(traceback.format_exc())
                
                # Ajouter l'image au dataset
                dataset.add_image(image)
            
            # Valider le dataset
            validation = dataset.validate_dataset()
            if not validation["valid"]:
                self.logger.warning(f"Validation du dataset échouée : {validation['errors']}")
                # On continue quand même, car certaines images peuvent être valides
            
            # Statistiques finales pour aider au debugging
            self.logger.info(
                f"Import terminé: {len(images)} images, {total_annotations} annotations, "
                f"{images_with_annotations} images avec annotations"
            )
            
            # Si aucune image n'a d'annotation, c'est probablement un problème
            if images_with_annotations == 0 and len(images) > 0:
                self.logger.error("AUCUNE IMAGE N'A D'ANNOTATION - Problème avec l'API ou le mapping des classes")
            
            return dataset
                
        except Exception as e:
            self.logger.error(f"Échec de l'import Mapillary : {str(e)}")
            import traceback
            self.logger.error(traceback.format_exc())
            raise ImportError(f"Échec de l'import Mapillary : {str(e)}")
    
    def import_from_local(
        self, 
        dataset: Dataset, 
        images_path: Union[str, Path], 
        annotations_path: Optional[Union[str, Path]] = None,
        format: DatasetFormat = DatasetFormat.YOLO
    ) -> Dataset:
        """
        Importe des images et annotations depuis un répertoire local
        
        Args:
            dataset: Dataset de destination
            images_path: Chemin vers les images
            annotations_path: Chemin vers les annotations (optionnel)
            format: Format des annotations
            
        Returns:
            Dataset mis à jour
        """
        try:
            # Convertir les chemins en Path
            images_path = Path(images_path)
            annotations_path = Path(annotations_path) if annotations_path else None
            
            # Vérifier que le chemin des images existe
            if not images_path.exists() or not images_path.is_dir():
                raise ImportError(f"Chemin d'images invalide : {images_path}")
            
            # Importer les images
            image_files = list(images_path.glob('*.[jJ][pP][gG]')) + \
                          list(images_path.glob('*.[pP][nN][gG]'))
            
            for image_file in image_files:
                # Charger et valider l'image
                try:
                    from PIL import Image as PILImage
                    with PILImage.open(image_file) as img:
                        width, height = img.size
                except Exception as e:
                    self.logger.warning(f"Impossible de charger l'image {image_file}: {str(e)}")
                    continue
                
                # Créer un objet Image
                image = Image(
                    id=image_file.stem,
                    path=image_file,
                    width=width,
                    height=height,
                    source=ImageSource.LOCAL
                )
                
                # Importer les annotations si possible
                if annotations_path:
                    self._import_annotations_for_image(
                        image, 
                        annotations_path / f"{image_file.stem}.txt", 
                        format,
                        dataset.classes
                    )
                
                # Ajouter l'image au dataset
                dataset.add_image(image)
            
            # Valider le dataset
            validation = dataset.validate_dataset()
            if not validation["valid"]:
                raise ValidationError(
                    f"Validation du dataset échouée : {validation['errors']}"
                )
            
            self.logger.info(f"Import de {len(dataset.images)} images")
            return dataset
        
        except Exception as e:
            self.logger.error(f"Échec de l'import local : {str(e)}")
            raise ImportError(f"Échec de l'import local : {str(e)}")
    
    def _import_annotations_for_image(
        self, 
        image: Image, 
        annotation_file: Path, 
        format: DatasetFormat,
        class_mapping: Dict[int, str]
    ):
        """
        Importe les annotations pour une image spécifique
        
        Args:
            image: Image à annoter
            annotation_file: Fichier d'annotations
            format: Format des annotations
            class_mapping: Mapping des classes
        """
        try:
            if not annotation_file.exists():
                return
            
            if format == DatasetFormat.YOLO:
                self._import_yolo_annotations(image, annotation_file, class_mapping)
            elif format == DatasetFormat.COCO:
                self._import_coco_annotations(image, annotation_file, class_mapping)
            elif format == DatasetFormat.VOC:
                self._import_voc_annotations(image, annotation_file, class_mapping)
        
        except Exception as e:
            self.logger.warning(f"Échec de l'import des annotations pour {image.path}: {str(e)}")
    
    def _import_yolo_annotations(
        self, 
        image: Image, 
        annotation_file: Path, 
        class_mapping: Dict[int, str]
    ):
        """
        Importe des annotations au format YOLO
        
        Format YOLO : class_id x_center y_center width height (normalized)
        """
        with open(annotation_file, 'r') as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) != 5:
                    continue
                
                try:
                    class_id = int(parts[0])
                    x_center, y_center, width, height = map(float, parts[1:])
                    
                    # Convertir du centre aux coordonnées normalisées
                    bbox = BoundingBox(
                        x=x_center - width/2,
                        y=y_center - height/2,
                        width=width,
                        height=height
                    )
                    
                    # Vérifier que la classe existe
                    if class_id not in class_mapping:
                        self.logger.warning(f"Classe {class_id} non définie pour l'image {image.path}")
                        continue
                    
                    # Créer l'annotation
                    annotation = Annotation(
                        class_id=class_id,
                        bbox=bbox,
                        type=AnnotationType.BBOX
                    )
                    
                    image.add_annotation(annotation)
                
                except ValueError as e:
                    self.logger.warning(f"Erreur de parsing de l'annotation : {str(e)}")
    
    def _import_coco_annotations(
        self, 
        image: Image, 
        annotation_file: Path, 
        class_mapping: Dict[int, str]
    ):
        """
        Importe des annotations au format COCO
        """
        with open(annotation_file, 'r') as f:
            coco_data = json.load(f)
        
        # Trouver les annotations pour cette image
        for ann in coco_data.get('annotations', []):
            # Vérifier que l'annotation correspond à l'image
            if ann.get('image_id') != image.id:
                continue
            
            # Extraire les informations de la bbox
            bbox = ann.get('bbox', [])
            if len(bbox) != 4:
                continue
            
            x, y, w, h = bbox
            
            # Convertir en coordonnées normalisées
            normalized_bbox = BoundingBox(
                x=x / image.width,
                y=y / image.height,
                width=w / image.width,
                height=h / image.height
            )
            
            # Récupérer l'ID de classe
            class_id = ann.get('category_id')
            if class_id not in class_mapping:
                self.logger.warning(f"Classe {class_id} non définie pour l'image {image.path}")
                continue
            
            # Créer l'annotation
            annotation = Annotation(
                class_id=class_id,
                bbox=normalized_bbox,
                type=AnnotationType.BBOX,
                confidence=ann.get('score', 1.0)
            )
            
            image.add_annotation(annotation)
    
    def _import_voc_annotations(
        self, 
        image: Image, 
        annotation_file: Path, 
        class_mapping: Dict[int, str]
    ):
        """
        Importe des annotations au format VOC (XML)
        """
        try:
            import xml.etree.ElementTree as ET
            
            tree = ET.parse(annotation_file)
            root = tree.getroot()
            
            # Extraire les informations de l'image
            size = root.find('size')
            if size is not None:
                width = int(size.find('width').text)
                height = int(size.find('height').text)
                
                # Vérifier la cohérence avec l'image
                if width != image.width or height != image.height:
                    self.logger.warning(f"Dimensions incohérentes pour {image.path}")
            
            # Parcourir les objets
            for obj in root.findall('object'):
                # Récupérer le nom de la classe
                name = obj.find('name').text
                
                # Trouver l'ID de classe correspondant
                class_id = None
                for id, class_name in class_mapping.items():
                    if class_name == name:
                        class_id = id
                        break
                
                if class_id is None:
                    self.logger.warning(f"Classe '{name}' non définie pour l'image {image.path}")
                    continue
                
                # Extraire les coordonnées de la bbox
                bbox_elem = obj.find('bndbox')
                x_min = int(bbox_elem.find('xmin').text)
                y_min = int(bbox_elem.find('ymin').text)
                x_max = int(bbox_elem.find('xmax').text)
                y_max = int(bbox_elem.find('ymax').text)
                
                # Convertir en coordonnées normalisées
                normalized_bbox = BoundingBox(
                    x=x_min / image.width,
                    y=y_min / image.height,
                    width=(x_max - x_min) / image.width,
                    height=(y_max - y_min) / image.height
                )
                
                # Créer l'annotation
                annotation = Annotation(
                    class_id=class_id,
                    bbox=normalized_bbox,
                    type=AnnotationType.BBOX
                )
                
                image.add_annotation(annotation)
        
        except Exception as e:
            self.logger.warning(f"Erreur lors de l'import des annotations VOC : {str(e)}")
    
    def import_dataset_config(
        self, 
        config_path: Union[str, Path]
    ) -> Dataset:
        """
        Importe un dataset à partir de son fichier de configuration
        
        Args:
            config_path: Chemin vers le fichier de configuration
            
        Returns:
            Dataset importé
        """
        try:
            # Convertir le chemin
            config_path = Path(config_path)
            
            # Vérifier l'existence du fichier
            if not config_path.exists():
                raise ImportError(f"Fichier de configuration non trouvé : {config_path}")
            
            # Charger le fichier de configuration
            with open(config_path, 'r', encoding='utf-8') as f:
                config_data = json.load(f)
            
            # Créer le dataset
            dataset = Dataset(
                name=config_data.get('name', 'Unnamed Dataset'),
                version=config_data.get('version', '1.0.0'),
                path=config_path.parent,
                classes=config_data.get('classes', {}),
                metadata=config_data.get('metadata', {})
            )
            
            # Importer les images si possible
            images_dir = config_path.parent / 'images'
            if images_dir.exists():
                self.import_from_local(
                    dataset, 
                    images_path=images_dir, 
                    annotations_path=config_path.parent / 'annotations'
                )
            
            self.logger.info(f"Configuration du dataset importée : {config_path}")
            return dataset
        
        except Exception as e:
            self.logger.error(f"Échec de l'import de la configuration : {str(e)}")
            raise ImportError(f"Échec de l'import de la configuration : {str(e)}")