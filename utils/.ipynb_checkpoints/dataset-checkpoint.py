import glob
import os
import random

import cv2
import numpy as np
import torch
import torch.nn.functional as F
from pycocotools import mask
from transformers import CLIPImageProcessor

from model.llava import conversation as conversation_lib
from model.llava.constants import (DEFAULT_IMAGE_TOKEN, IGNORE_INDEX,
                                   IMAGE_TOKEN_INDEX)
from model.llava.mm_utils import tokenizer_image_token
from model.segment_anything.utils.transforms import ResizeLongestSide

from .conversation import get_default_conv_template
from .data_processing import get_mask_from_json
from .reason_seg_dataset import ReasonSegDataset
from .refer import REFER
from .refer_seg_dataset import ReferSegDataset
from .sem_seg_dataset import SemSegDataset
from .utils import (DEFAULT_IM_END_TOKEN, DEFAULT_IM_START_TOKEN,
                    DEFAULT_IMAGE_TOKEN, ANSWER_LIST)
from .vqa_dataset import VQADataset


def collate_fn(
    batch, tokenizer=None, conv_type="llava_v1", use_mm_start_end=True
):
    image_path_list = []
    images_list = []
    images_clip_list = []
    conversation_list = []
    masks_list = []
    label_list = []
    resize_list = []
    questions_list = []
    sampled_classes_list = []
    offset_list = [0]
    cnt = 0
    inferences = []
    text_only_list = []
    for (
        image_path,
        images,
        images_clip,
        conversations,
        masks,
        label,
        resize,
        questions,
        sampled_classes,
        inference,
        text_only
    ) in batch:
        image_path_list.append(image_path)
        images_list.append(images)
        images_clip_list.append(images_clip)
        conversation_list.extend(conversations)
        label_list.append(label)
        masks_list.append(masks.float())
        resize_list.append(resize)
        questions_list.append(questions)
        sampled_classes_list.append(sampled_classes)
        # print('conversations: ', conversations)
        cnt += len(conversations)
        offset_list.append(cnt)
        inferences.append(inference)
        text_only_list.append(text_only)
    # print(f'image paths: {image_path_list}')
    # print(f'conversation_list: {conversation_list}')
    if use_mm_start_end:
        # replace <image> token

        # Conversation_list: DIM (3*B)
        for i in range(len(conversation_list)):
            # We include the some useful extra tokens
            replace_token = DEFAULT_IMAGE_TOKEN
            replace_token = (
                DEFAULT_IM_START_TOKEN + replace_token + DEFAULT_IM_END_TOKEN
            )
            conversation_list[i] = conversation_list[i].replace(
                DEFAULT_IMAGE_TOKEN, replace_token
            )
    # input_ids corresponds to the tokenized prompt (B*3 objects)
    input_ids = [
        tokenizer_image_token(prompt, tokenizer, return_tensors="pt")
        for prompt in conversation_list
    ]
    
    # for prompt in conversation_list:
    #     print('collate prompt: ', prompt)

    # Padding sequences of token IDs to make them uniform in length
    input_ids = torch.nn.utils.rnn.pad_sequence(
        input_ids, batch_first=True, padding_value=tokenizer.pad_token_id
    )
    # Create attention masks to indicate which tokens are padding
    attention_masks = input_ids.ne(tokenizer.pad_token_id)

    # Load some default conversation
    # conv = conversation_lib.default_conversation.copy()
    conv = conversation_lib.default_conversation_medical.copy()

    # Copy the conversation
    targets = input_ids.clone()

    if conv_type == "llava_v1":
        sep = conv.sep + conv.roles[1] + ": "
    else:
        sep = "[/INST] "
    for conversation, target in zip(conversation_list, targets):
        total_len = int(target.ne(tokenizer.pad_token_id).sum())

        rounds = conversation.split(conv.sep2)
        cur_len = 1
        target[:cur_len] = IGNORE_INDEX
        for i, rou in enumerate(rounds):
            if rou == "":
                break

            parts = rou.split(sep)
            # print('parts', parts)
            # if len(parts) != 2:
            #     break
            assert len(parts) == 2, (len(parts), rou)
            parts[0] += sep

            if DEFAULT_IMAGE_TOKEN in conversation:
                round_len = len(tokenizer_image_token(rou, tokenizer))
                instruction_len = len(tokenizer_image_token(parts[0], tokenizer)) - 2
            else:
                round_len = len(tokenizer(rou).input_ids)
                instruction_len = len(tokenizer(parts[0]).input_ids) - 2

            target[cur_len : cur_len + instruction_len] = IGNORE_INDEX

            cur_len += round_len
        target[cur_len:] = IGNORE_INDEX

        if cur_len < tokenizer.model_max_length:
            assert cur_len == total_len

    if inferences[0] == False:
        truncate_len = tokenizer.model_max_length 


        if input_ids.shape[1] > truncate_len:
            # input_id, targets and attention_masks are based on the prompt
            input_ids = input_ids[:, :truncate_len]
            targets = targets[:, :truncate_len]
            attention_masks = attention_masks[:, :truncate_len]
#     print('collate input_ids: ', input_ids, len(input_ids), input_ids.shape)
#     print('collate target: ', targets, len(targets), targets.shape)

#     print('collate input_ids: ', tokenizer.decode(input_ids))
#     print('collate target: ', tokenizer.decode(targets))
    return {
        "image_paths": image_path_list,
        "images": torch.stack(images_list, dim=0),
        "images_clip": torch.stack(images_clip_list, dim=0),
        "input_ids": input_ids,
        "labels": targets,
        "attention_masks": attention_masks,
        "masks_list": masks_list,
        "label_list": label_list,
        "resize_list": resize_list,
        "offset": torch.LongTensor(offset_list),
        "questions_list": questions_list,
        "sampled_classes_list": sampled_classes_list,
        "inference": inferences[0],
        "conversation_list": conversation_list,
        'text_only': text_only_list
    }



class HybridDataset(torch.utils.data.Dataset):
    pixel_mean = torch.Tensor([123.675, 116.28, 103.53]).view(-1, 1, 1)
    pixel_std = torch.Tensor([58.395, 57.12, 57.375]).view(-1, 1, 1)
    img_size = 1024
    ignore_label = 255

    def __init__(
        self,
        base_image_dir,
        tokenizer,
        vision_tower,
        samples_per_epoch=500 * 8 * 2 * 10,
        precision: str = "fp32",
        image_size: int = 224,
        num_classes_per_sample: int = 3,
        exclude_val=False,
        dataset="sem_seg||refer_seg||vqa||reason_seg",
        sample_rate=[9, 3, 3, 1],
        sem_seg_data="ade20k||cocostuff||partimagenet||pascal_part||paco_lvis||mapillary",
        refer_seg_data="refclef||refcoco||refcoco+||refcocog",
        vqa_data="llava_instruct_150k",
        reason_seg_data="ReasonSeg|train",
        explanatory=0.1,
    ):
        self.exclude_val = exclude_val
        self.dataset = dataset
        self.samples_per_epoch = samples_per_epoch
        self.explanatory = explanatory
        self.num_classes_per_sample = num_classes_per_sample
        sample_rate = np.array(sample_rate)
        self.sample_rate = sample_rate / sample_rate.sum()

        self.base_image_dir = base_image_dir
        self.image_size = image_size
        self.tokenizer = tokenizer
        self.precision = precision

        self.datasets = dataset.split("||")

        self.all_datasets = []
        for dataset in self.datasets:
            if dataset == "sem_seg":
                self.all_datasets.append(
                    SemSegDataset(
                        base_image_dir,
                        tokenizer,
                        vision_tower,
                        samples_per_epoch,
                        precision,
                        image_size,
                        num_classes_per_sample,
                        exclude_val,
                        sem_seg_data,
                    )
                )
            elif dataset == "refer_seg":
                self.all_datasets.append(
                    ReferSegDataset(
                        base_image_dir,
                        tokenizer,
                        vision_tower,
                        samples_per_epoch,
                        precision,
                        image_size,
                        num_classes_per_sample,
                        exclude_val,
                        refer_seg_data,
                    )
                )
            elif dataset == "vqa":
                self.all_datasets.append(
                    VQADataset(
                        base_image_dir,
                        tokenizer,
                        vision_tower,
                        samples_per_epoch,
                        precision,
                        image_size,
                        num_classes_per_sample,
                        exclude_val,
                        vqa_data,
                    )
                )
            elif dataset == "reason_seg":
                self.all_datasets.append(
                    ReasonSegDataset(
                        base_image_dir,
                        tokenizer,
                        vision_tower,
                        samples_per_epoch,
                        precision,
                        image_size,
                        num_classes_per_sample,
                        exclude_val,
                        reason_seg_data,
                        explanatory,
                    )
                )

    def __len__(self):
        return self.samples_per_epoch

    def __getitem__(self, idx):
        ind = np.random.choice(list(range(len(self.datasets))), p=self.sample_rate)
        data = self.all_datasets[ind]
        inference = False
        return *data[0], inference


class ValDataset(torch.utils.data.Dataset):
    pixel_mean = torch.Tensor([123.675, 116.28, 103.53]).view(-1, 1, 1)
    pixel_std = torch.Tensor([58.395, 57.12, 57.375]).view(-1, 1, 1)
    img_size = 1024
    ignore_label = 255

    def __init__(
        self,
        base_image_dir,
        tokenizer,
        vision_tower,
        val_dataset,
        image_size=1024,
    ):
        self.base_image_dir = base_image_dir
        splits = val_dataset.split("|")
        if len(splits) == 2:
            ds, split = splits
            images = glob.glob(
                os.path.join(self.base_image_dir, "reason_seg", ds, split, "*.jpg")
            )
            self.images = images
            self.data_type = "reason_seg"
        elif len(splits) == 3:
            ds, splitBy, split = splits
            refer_api = REFER(self.base_image_dir, ds, splitBy)
            ref_ids_val = refer_api.getRefIds(split=split)
            images_ids_val = refer_api.getImgIds(ref_ids=ref_ids_val)
            refs_val = refer_api.loadRefs(ref_ids=ref_ids_val)
            refer_seg_ds = {}
            refer_seg_ds["images"] = []
            loaded_images = refer_api.loadImgs(image_ids=images_ids_val)
            for item in loaded_images:
                item = item.copy()
                if ds == "refclef":
                    item["file_name"] = os.path.join(
                        base_image_dir, "images/saiapr_tc-12", item["file_name"]
                    )
                elif ds in ["refcoco", "refcoco+", "refcocog", "grefcoco"]:
                    item["file_name"] = os.path.join(
                        base_image_dir,
                        "images/mscoco/images/train2014",
                        item["file_name"],
                    )
                refer_seg_ds["images"].append(item)
            refer_seg_ds["annotations"] = refer_api.Anns  # anns_val

            img2refs = {}
            for ref in refs_val:
                image_id = ref["image_id"]
                img2refs[image_id] = img2refs.get(image_id, []) + [
                    ref,
                ]
            refer_seg_ds["img2refs"] = img2refs
            self.refer_seg_ds = refer_seg_ds
            self.data_type = "refer_seg"

        self.ds = ds
        self.image_size = image_size
        self.tokenizer = tokenizer
        self.transform = ResizeLongestSide(image_size)
        self.clip_image_processor = CLIPImageProcessor.from_pretrained(vision_tower)

    def __len__(self):
        if self.data_type == "refer_seg":
            return len(self.refer_seg_ds["images"])
        else:
            return len(self.images)

    def preprocess(self, x: torch.Tensor) -> torch.Tensor:
        """Normalize pixel values and pad to a square input."""
        # Normalize colors
        x = (x - self.pixel_mean) / self.pixel_std

        # Pad
        h, w = x.shape[-2:]
        padh = self.img_size - h
        padw = self.img_size - w
        x = F.pad(x, (0, padw, 0, padh))
        return x

    def __getitem__(self, idx):
        if self.data_type == "refer_seg":
            refer_seg_ds = self.refer_seg_ds
            images = refer_seg_ds["images"]
            annotations = refer_seg_ds["annotations"]
            img2refs = refer_seg_ds["img2refs"]

            image_info = images[idx]
            image_path = image_info["file_name"]
            image_id = image_info["id"]

            refs = img2refs[image_id]
            if len(refs) == 0:
                raise ValueError("image {} has no refs".format(image_id))

            sents = []
            ann_ids = []
            for ref in refs:
                for sent in ref["sentences"]:
                    sents.append(sent["sent"].strip().lower())
                    ann_ids.append(ref["ann_id"])

            sampled_sents = sents
            sampled_ann_ids = ann_ids
            image = cv2.imread(image_path)
            image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            is_sentence = False
        else:
            image_path = self.images[idx]
            image = cv2.imread(image_path)
            image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            json_path = image_path.replace(".jpg", ".json")
            mask_json, sampled_sents, is_sentence = get_mask_from_json(json_path, image)
            sampled_sents = [sampled_sents[0]]

        conversations = []
        conv = conversation_lib.default_conversation.copy()
        i = 0
        while i < len(sampled_sents):
            conv.messages = []
            text = sampled_sents[i].strip()
            if is_sentence:
                conv.append_message(
                    conv.roles[0],
                    DEFAULT_IMAGE_TOKEN
                    + "\n {} Please output segmentation mask.".format(text),
                )
                conv.append_message(conv.roles[1], "[SEG].")
            else:
                conv.append_message(
                    conv.roles[0],
                    DEFAULT_IMAGE_TOKEN
                    + "\n What is {} in this image? Please output segmentation mask.".format(
                        text
                    ),
                )
                conv.append_message(conv.roles[1], "[SEG].")
            conversations.append(conv.get_prompt())
            i += 1

        # preprocess image for clip
        image_clip = self.clip_image_processor.preprocess(image, return_tensors="pt")[
            "pixel_values"
        ][0]

        # preprocess image for sam
        image = self.transform.apply_image(image)
        resize = image.shape[:2]
        image = self.preprocess(torch.from_numpy(image).permute(2, 0, 1).contiguous())

        if self.data_type == "refer_seg":
            masks = []
            for i, ann_id in enumerate(sampled_ann_ids):
                ann = annotations[ann_id]
                if len(ann["segmentation"]) == 0 and sampled_sents[i] != "":
                    m = np.zeros((image_info["height"], image_info["width"], 1))
                else:
                    if type(ann["segmentation"][0]) == list:  # polygon
                        rle = mask.frPyObjects(
                            ann["segmentation"],
                            image_info["height"],
                            image_info["width"],
                        )
                    else:
                        rle = ann["segmentation"]
                        for i in range(len(rle)):
                            if not isinstance(rle[i]["counts"], bytes):
                                rle[i]["counts"] = rle[i]["counts"].encode()
                    m = mask.decode(rle)
                m = np.sum(
                    m, axis=2
                )  # sometimes there are multiple binary map (corresponding to multiple segs)
                m = m.astype(np.uint8)  # convert to np.uint8
                masks.append(m)
        else:
            masks = [mask_json]

        masks = np.stack(masks, axis=0)
        masks = torch.from_numpy(masks)
        labels = torch.ones(masks.shape[1], masks.shape[2]) * self.ignore_label
        inference = True

        return (
            image_path,
            image,
            image_clip,
            conversations,
            masks,
            labels,
            resize,
            None,
            None,
            inference,
        )


class TumorDataset(torch.utils.data.Dataset):
    pixel_mean = torch.Tensor([123.675, 116.28, 103.53]).view(-1, 1, 1)
    pixel_std = torch.Tensor([58.395, 57.12, 57.375]).view(-1, 1, 1)
    img_size = 1024
    ignore_label = 255

    def __init__(
        self,
        base_image_dir,
        tokenizer,
        vision_tower,
        precision: str = "fp32",
        image_size: int = 224,
        exclude_val=False,
        mode = "Training", # Training, Val, Test 
        num_classes = 3,
        just_using_tumor_imgs = True,
        just_use_pos_cases = True,
        merging_classes = True
    ):
        self.exclude_val = exclude_val
        self.base_image_dir = base_image_dir
        self.image_size = image_size
        self.tokenizer = tokenizer
        self.precision = precision
        self.num_classes = num_classes
        self.just_use_pos_cases = just_use_pos_cases
        self.merging_classes = merging_classes
        self.transform = ResizeLongestSide(image_size)
        self.clip_image_processor = CLIPImageProcessor.from_pretrained(vision_tower)

        self.mode_path = os.path.join(base_image_dir,mode)
        # Obtain the main images
        self.img_paths = os.listdir(os.path.join(self.mode_path,"image"))

        # List to hold file paths
        self.all_slices = []
        
        # Walk through the directory and its subdirectories
        for root, dirs, slices in os.walk(os.path.join(self.mode_path,"image")):
            for slice in slices:
                # Get the full slice path
                slice_path = os.path.join(root, slice)
                self.all_slices.append(slice_path)

        # Provide just the slices with tumors 
        if just_using_tumor_imgs:
            slices_with_tumors = []
            self.clasess_available = []
            for slice in self.all_slices:
                mask = np.load(slice.replace("/image/","/mask/").replace(".jpg",".npy"))
                has_values_over_zero = np.any(mask > 0)
                if has_values_over_zero:
                    slices_with_tumors.append(slice)
                    classes_per_slice = np.unique(mask)
                    self.clasess_available.append(classes_per_slice[classes_per_slice != 0].tolist())
        self.all_slices = slices_with_tumors

        print("len(self.all_slices): ", len(self.all_slices))

        # Define input statements
        if self.merging_classes:
            self.sentences = ["The whole tumor includes all visible tumor regions, including the actively growing enhanced tumor, the surrounding non-enhancing tumor tissue, and any peritumoral edema. Is there any region that indicates the presence of a tumor?. Provide the segmentation mask."]
        else:
            self.sentences = [
                """The enhanced tumor forms the central, actively growing portion of the tumor. Is there any enhanced tumor region? Provide the segmentation mask.""",
                """Non-enhanced tumor including necrotic and cystic areas, often surrounds the enhanced tumor, forming the inner boundary of the tumor mass. Is there any Non-enhanced tumor region? Provide the segmentation mask.""",
                """Edema forms the outermost region, extending into surrounding brain tissues as a more diffuse, swollen area with fluid accumulation. Is there any edema region? Provide the segmentation mask."""
            ]

    def __len__(self):
        return len(self.all_slices)

    def preprocess(self, x: torch.Tensor) -> torch.Tensor:
        """Normalize pixel values and pad to a square input."""
        # Normalize colors
        x = (x - self.pixel_mean) / self.pixel_std

        # Pad
        h, w = x.shape[-2:]
        padh = self.img_size - h
        padw = self.img_size - w
        x = F.pad(x, (0, padw, 0, padh))
        return x

    def __getitem__(self, idx):
        
        # Select a volumetric image
        index = idx 
        slice_path = self.all_slices[index]
        image = cv2.imread(slice_path, cv2.IMREAD_GRAYSCALE)
        # Replicate the grayscale image across three channels to simulate an RGB image
        image = np.stack([image] * 3, axis=-1)
        mask = np.load(slice_path.replace("/image/","/mask/").replace(".jpg",".npy"))
        captions = []
        with open(slice_path.replace("/image/","/captions_per_slice/").replace(".jpg",".txt"), 'r') as file:
                    # Iterate over each line in the file
                    for line in file:
                        # Strip trailing newline characters and add to the list
                        captions.append(line.strip())
        # preprocess image for clip
        image_clip = self.clip_image_processor.preprocess(image, return_tensors="pt")[
            "pixel_values"][0]
        
        if self.merging_classes:
            selected_class = 1
            question = self.sentences[selected_class-1]
        else:
            # There are three classes of tumors. Select randomly one
            if self.just_use_pos_cases:
                classes = self.clasess_available[index]
                selected_class = random.choice(classes)
                # Our defined classes start from zero
                selected_class = int(selected_class) - 1
            else:        
                selected_class = np.random.randint(0,self.num_classes)
            question = self.sentences[selected_class]
        questions = [DEFAULT_IMAGE_TOKEN + "\n" + question]
        caption = captions[selected_class]
        if self.merging_classes:
            mask = np.where(mask > 0, 1, 0)
        else:
            mask = np.where(mask == selected_class+1, 1, 0)
        image = self.transform.apply_image(image)  # preprocess image for sam
        resize = image.shape[:2]

        conversations = []
        conv = conversation_lib.default_conversation.copy()
        answers = ANSWER_LIST
        conv.messages = []
        conv.append_message(conv.roles[0], questions[0])
        conv.append_message(conv.roles[1], answers[0])
        conversations.append(conv.get_prompt())

        # Generate final outputs
        image = self.preprocess(torch.from_numpy(image).permute(2, 0, 1).contiguous())
        masks = np.stack([mask] * 3, axis=0)
        masks = torch.from_numpy(masks)
        label = torch.ones(masks.shape[1], masks.shape[2]) * self.ignore_label
        conversations = conversations * 3
        inference = False

        return (
            slice_path,
            image,
            image_clip,
            conversations,
            masks,
            label,
            resize,
            questions,
            None,
            inference
        )


class ValTumorDataset(torch.utils.data.Dataset):
    pixel_mean = torch.Tensor([123.675, 116.28, 103.53]).view(-1, 1, 1)
    pixel_std = torch.Tensor([58.395, 57.12, 57.375]).view(-1, 1, 1)
    img_size = 1024
    ignore_label = 255

    def __init__(
        self,
        base_image_dir,
        tokenizer,
        vision_tower,
        mode,
        image_size=1024,
        num_classes=3,
        just_using_tumor_imgs = True,
        just_use_pos_cases = True,
        merging_classes = True
    ):
       
        self.base_image_dir = base_image_dir
        self.image_size = image_size
        self.tokenizer = tokenizer
        self.num_classes = num_classes
        self.just_use_pos_cases = just_use_pos_cases
        self.merging_classes = merging_classes
        self.transform = ResizeLongestSide(image_size)
        self.clip_image_processor = CLIPImageProcessor.from_pretrained(vision_tower)
        self.mode_path = os.path.join(base_image_dir,mode)
        # Obtain the main images
        self.img_paths = os.listdir(os.path.join(self.mode_path,"image"))

        # List to hold file paths
        self.all_slices = []
        
        # Walk through the directory and its subdirectories
        for root, dirs, slices in os.walk(os.path.join(self.mode_path,"image")):
            for slice in slices:
                # Get the full slice path
                slice_path = os.path.join(root, slice)
                self.all_slices.append(slice_path)

        # Provide just the slices with tumors 
        if just_using_tumor_imgs:
            slices_with_tumors = []
            self.clasess_available = []
            for slice in self.all_slices:
                mask = np.load(slice.replace("/image/","/mask/").replace(".jpg",".npy"))
                has_values_over_zero = np.any(mask > 0)
                if has_values_over_zero:
                    slices_with_tumors.append(slice)
                    classes_per_slice = np.unique(mask)
                    self.clasess_available.append(classes_per_slice[classes_per_slice != 0].tolist())
        self.all_slices = slices_with_tumors

        print("len(self.all_slices): ", len(self.all_slices))

        # Define input statements
        self.sentences = [
            """The enhanced tumor forms the central, actively growing portion of the tumor. Is there any enhanced tumor region? Provide the segmentation mask.""",
            """Non-enhanced tumor including necrotic and cystic areas, often surrounds the enhanced tumor, forming the inner boundary of the tumor mass. Is there any Non-enhanced tumor region? Provide the segmentation mask.""",
            """Edema forms the outermost region, extending into surrounding brain tissues as a more diffuse, swollen area with fluid accumulation. Is there any edema region? Provide the segmentation mask."""
        ]

    def __len__(self):
        return len(self.all_slices)

    def preprocess(self, x: torch.Tensor) -> torch.Tensor:
        """Normalize pixel values and pad to a square input."""
        # Normalize colors
        x = (x - self.pixel_mean) / self.pixel_std

        # Pad
        h, w = x.shape[-2:]
        padh = self.img_size - h
        padw = self.img_size - w
        x = F.pad(x, (0, padw, 0, padh))
        return x

    def __getitem__(self, idx):
        
        # Select a volumetric image
        index = idx 
        slice_path = self.all_slices[index]
        image = cv2.imread(slice_path, cv2.IMREAD_GRAYSCALE)
        # Replicate the grayscale image across three channels to simulate an RGB image
        image = np.stack([image] * 3, axis=-1)
        mask = np.load(slice_path.replace("/image/","/mask/").replace(".jpg",".npy"))
        captions = []
        with open(slice_path.replace("/image/","/captions_per_slice/").replace(".jpg",".txt"), 'r') as file:
                    # Iterate over each line in the file
                    for line in file:
                        # Strip trailing newline characters and add to the list
                        captions.append(line.strip())
        # preprocess image for clip
        image_clip = self.clip_image_processor.preprocess(image, return_tensors="pt")[
            "pixel_values"][0]
        if self.merging_classes:
            selected_class = 1
            question = self.sentences[selected_class-1]
        else:
            # There are three classes of tumors. Select randomly one
            if self.just_use_pos_cases:
                classes = self.clasess_available[index]
                selected_class = random.choice(classes)
                # Our defined classes start from zero
                selected_class = int(selected_class)-1
            else:        
                selected_class = np.random.randint(0,self.num_classes)
            question = self.sentences[selected_class]
        questions = [DEFAULT_IMAGE_TOKEN + "\n" + question]
        caption = captions[selected_class]
        if self.merging_classes:
            mask = np.where(mask > 0, 1, 0)
        else:
            mask = np.where(mask == selected_class+1, 1, 0)
        image = self.transform.apply_image(image)  # preprocess image for sam
        resize = image.shape[:2]

        conversations = []
        conv = conversation_lib.default_conversation.copy()
        answers = ANSWER_LIST
        conv.messages = []
        conv.append_message(conv.roles[0], questions[0])
        conv.append_message(conv.roles[1], answers[0])
        conversations.append(conv.get_prompt())

        # Generate final outputs
        image = self.preprocess(torch.from_numpy(image).permute(2, 0, 1).contiguous())
        masks = np.stack([mask] * 3, axis=0)
        masks = torch.from_numpy(masks)
        label = torch.ones(masks.shape[1], masks.shape[2]) * self.ignore_label
        conversations = conversations
        inference = True

        return (
            slice_path,
            image,
            image_clip,
            conversations,
            masks,
            label,
            resize,
            questions,
            None,
            inference
        )