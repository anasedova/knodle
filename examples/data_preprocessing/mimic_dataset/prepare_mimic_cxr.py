# -*- coding: utf-8

"""
Preprocessing of MIMIC-CXR dataset

This file illustrates how week supervision can be applied on medical images 
and the corresponding reports. Since there are two sources of data (images and 
reports) we establish a double layer week supervision. 

In this example the MIMIC-CXR dataset is used. There are to versions of this 
dataset: 

[MIMIC-CXR](https://physionet.org/content/mimic-cxr/2.0.0/) Database (Johnson, 
Pollard et al. (2019) is a large publicly available dataset of chest X-rays 
including radiology reports. It contains 377110 images and 227835 radiographic 
. A radiographic study consists of one report and one or multiple images. 

[MIMIC-CXR-JPG](https://physionet.org/content/mimic-cxr-jpg/2.0.0/) Database 
(Johnson, Lungren et al. (2019) bases on MIMIC-CXR. It additionally includes 
weak labels which are derived from the radiology reports using CheXpert labler 
(Irvin, Rajpurkar et al. 2019) and the images are in JPG format instead of 
DICOM format. 

Neiter versions of the MIMIC-CXR dataset have gold labels. Since both the 
CheXpert data and the MIMIC-CXR data contain chest X-Rays, the CheXpert labler 
was used in the MIMIC-CXR-JPG Database to obtain week labels. We will use a 
small subset of the MIMIC images and their week labels in the data 
preprocessing to finetune our image encoder CNN. Apart from that we do not 
touch any labels until evaluation.
To evaluate our results in the end, we apply the trained model (Knodle output) 
to the validation data of the CheXpert dataset, since they have gold labels. 

In the data preprocessing we build the three input matrices knodle requires:
 * The rules are generated from the CheXpert Labler phrases. The phrases 
   contain mentions (synonyms or related words) for each class, which we use to 
   build our T matrix, so the "rule to class" matrix.
 * The Z matrix, so the "rule matches" matrix is generated from the reports 
   and the rules. 
 * The images are encoded with a CNN. We try two different approaches: 
     1) CNN with pretrained weight without finetuning and 
    2) CNN with pretrained weights and finetuning. Therefore, we need the week 
       labels.  

"""

# set directory
# os.chdir("")

# set n between 1 and 377110
n = 1000
# PhysioNet
USERNAME = "your_username_her"
PASSWORD = "your_pw_here"

import os
from tqdm import tqdm

import numpy as np
import pandas as pd
import random
import copy
import csv
import itertools

import torch
import torchvision.transforms as transforms
import torch.nn as nn
import torchvision.models as models
import torch.optim as optim
from torch.utils.data import DataLoader, Dataset
from typing import Dict
from joblib import dump, load
from PIL import Image


# downloads from mimic-cxr
url = ["wget -N -c -np --user=", USERNAME, " --password=", PASSWORD, 
       " https://physionet.org/files/mimic-cxr/2.0.0/"]

command = "".join(url+["cxr-record-list.csv.gz"]) # paths to images
os.system(command)
command = "".join(url+["cxr-study-list.csv.gz"]) # paths to reports
os.system(command)
command = "".join(url+["mimic-cxr-reports.zip"]) # folder of all reports
os.system(command)

# downloads from mimic-cxr-jpg
url = ["wget -N -c -np --user=", USERNAME, " --password=", PASSWORD, 
       " https://physionet.org/files/mimic-cxr-jpg/2.0.0/"]
command = "".join(url+["mimic-cxr-2.0.0-chexpert.csv.gz"]) # chexpert output 
                                                           # for mimic dataset
os.system(command)

#NOW UNZIP ALL DOWNLOADED FILES AND THE REPORT FOLDER

##############################################################################
# MIMIC-CXR-JPG images
############################################################################## 
record_list = pd.read_csv("cxr-record-list.csv").to_numpy()
study_list = pd.read_csv("cxr-study-list.csv").to_numpy()


# image download - run only once
for i in tqdm(range(1000,n)):
    url = ["wget -N -c -np --user=", USERNAME, " --password=", PASSWORD, 
           " https://physionet.org/files/mimic-cxr-jpg/2.0.0/",record_list[i,3]]
    command = "".join(url)
    command = "".join([command.replace(".dcm", ""),".jpg -P ",record_list[i,3]])
    os.system(command)
        
    
with open('mimic_cxr_text.csv', 'w', newline='', encoding='utf-8') as f:
    for i in tqdm(range(len(study_list))):
        with open(''.join(["mimic-cxr-reports/", study_list[i,2]])) as f_path:
            text = ''.join(f_path.readlines())
        text = text.replace("\n", "")
        text = text.replace(",", "")
        start = text.find("FINDINGS:")
        end = text.find("IMPRESSION:")
        findings = text[start:end]
        impressions = text[end:len(text)]
        row = [study_list[i,0],study_list[i,1], findings, impressions]
        csvwriter = csv.writer(f)
        csvwriter.writerow(row)

# open
reports = pd.read_csv("mimic_cxr_text.csv", 
                      names = ["subject_id","study_id", "findings", "impressions"], 
                      na_values='.')

print("average length findings section:", 
      np.mean(reports["findings"].str.len()))

print("average length impression section:", 
      np.mean(reports["impressions"].str.len()))

print("number of NAs in findings and impressions:", 
      pd.isna(reports[['findings', 'impressions']]).sum())

# if impression is missing insert finding
reports.impressions.fillna(reports.findings, inplace=True)
#if neither are there, we do not analyse this study -> drop
del reports['findings']
reports_processed = reports.dropna()

# merge reports to record_list
record_list = pd.read_csv("cxr-record-list.csv")
record_report_list = pd.merge(record_list, reports_processed, 
                              how = 'left', on= ['study_id','subject_id'])

##############################################################################
# labels 
##############################################################################
labels_chexpert = pd.read_csv("mimic-cxr-2.0.0-chexpert.csv")
# initialise labels with 0
labels_chexpert['label'] = 0
labels_list = labels_chexpert.columns.to_numpy()
# iterate through labels: 
# three cases: only one, non, or multiple diagnoses
for i in tqdm(range(len(labels_chexpert))):
    # which labels are 1? 
    label_is1 = labels_chexpert.iloc[i,:] == 1.0
    if (sum(label_is1)==1):
       labels_chexpert.iloc[i,16] = labels_list[label_is1]
    elif sum(label_is1) > 1:
        labels_chexpert.iloc[i,16] = random.choice(labels_list[label_is1])
    else: 
        labels_chexpert.iloc[i,16] = 'No Finding'
        
labels = {id: cat for (cat, id) in enumerate(labels_chexpert.columns[2:16])}
        
for i in tqdm(range(len(labels_chexpert))):
 labels_chexpert.iloc[i,16] = labels.get(labels_chexpert.iloc[i,16])
        
# merge labels with records and reports
record_report_label_list = pd.merge(record_report_list, 
                                    labels_chexpert.iloc[:,[0,1,16]], 
                                    how = 'left', 
                                    on = ['study_id','subject_id'])

print("classes proportions:", 
      record_report_label_list.groupby('label').size()/len(record_report_label_list))
# keep in mind that the dataset is unbalenced


input_list_full = record_report_label_list
# save the whole file
dump(input_list_full, "input_list.lib")
# open only first n rows
input_list_pd = load("input_list.lib").iloc[:n,:]
# drop nas
input_list = input_list_pd.dropna().to_numpy()
# save new n
n = len(input_list)
##############################################################################
# make rules from reports and Chexpert-labler
##############################################################################
# lower case & replace whitespace with _
classes = [string.lower().replace(" ", "_") for string in labels]
num_classes = len(classes)
labels2ids = {classes[i]:i for i in range(num_classes)}
# create folder
os.makedirs("".join([os.getcwd(),"/chexpert_rules"]))
# store files in folder
for i in range(len(classes)):
    os.system("".join(["curl https://raw.githubusercontent.com/stanfordmlgroup/chexpert-labeler/master/phrases/mention/", 
                       classes[i], ".txt ", "-o chexpert_rules/", classes[i], ".txt"]))

# make T matrix
lines = {}
for i in range(len(classes)):
    with open("".join(["chexpert_rules/", classes[i], ".txt"])) as f:
        lines[classes[i]] = [each_string.replace("\n", "") for each_string in f.readlines()]
          
mentions = pd.DataFrame({'label': label, 'rule': rule} for (label, rule) in lines.items())
mentions.head()

rules = pd.DataFrame([i for i in itertools.chain.from_iterable(mentions['rule'])], columns = ["rule"])
rules['rule_id'] = range(len(rules))
rules['label'] = np.concatenate([
    np.repeat(mentions['label'][i], len(mentions['rule'][i])) for i in range(num_classes)])
rules['label_id'] = [labels2ids[rules['label'][i]] for i in range(len(rules))]
rules.head()

rule2rule_id = dict(zip(rules["rule"], rules["rule_id"]))
rule2label = dict(zip(rules["rule_id"], rules["label_id"]))

def get_mapping_rules_labels_t(rule2label: Dict, num_classes: int) -> np.ndarray:
    """ Function calculates t matrix (rules x labels) using the known correspondence of relations to decision rules """
    mapping_rules_labels_t = np.zeros([len(rule2label), num_classes])
    for rule, labels in rule2label.items():
        mapping_rules_labels_t[rule, labels] = 1
    return mapping_rules_labels_t

mapping_rules_labels_t = get_mapping_rules_labels_t(rule2label, len(labels2ids))
mapping_rules_labels_t[0:5,:]
mapping_rules_labels_t.shape


len(np.unique(rules['rule'])) == len(rules['rule'])
rules_size = rules.groupby('rule').size() 
rules_size[np.where(rules_size > 1)[0]]
# rule defib appears for two different classes

# make Z matrix     
def get_rule_matches_z (data: np.ndarray, num_rules: int) -> np.ndarray:
    """
    Function calculates the z matrix (samples x rules)
    data: np.array (reports)
    output: sparse z matrix
    """
    rule_matches_z = np.zeros([len(data), num_rules])
    for ind in range(len(data)):
        for rule, rule_id in rule2rule_id.items():
            if rule in (data[ind]):
                rule_matches_z[ind, rule_id] = 1
    return rule_matches_z

rule_matches_z = get_rule_matches_z(input_list[:,4], (len(rule2rule_id)+1))

dump(rule_matches_z, "rule_matches_z.lib")
######################################################################
# image - encoding: 
# without finetuning
######################################################################
class mimicDataset(Dataset):
    
    def __init__(self, path):
        'Initialization'
        self.path = path
        #self.y = y
        
    def __len__(self):
        'Denotes the total number of samples'
        return len(self.path)
    
    def __getitem__(self, index):
        'Generates one sample of data'
        # Select sample
        image = Image.open(self.path[index,3].replace(".dcm", ".jpg")).convert('RGB')
        X = self.transform(image)
        label = self.path[index,5]
        
        return X, torch.tensor(label)
    
    transform = transforms.Compose([
        transforms.Resize(256),
        transforms.CenterCrop(224),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])])

model = models.resnet50(pretrained=True)
modules = list(model.children())[:-1]
model=torch.nn.Sequential(*modules)
for p in model.parameters():
    p.requires_grad = False
    
model.eval()
# apply modified resnet50 to data
dataloaders = DataLoader(mimicDataset(input_list[:n,:]), batch_size=n,num_workers=0)
    
data, labels = next(iter(dataloaders))
with torch.no_grad():
    features_var = model(data)
    features = features_var.data 
    all_X = features.reshape(n,2048).numpy()

# save feature matrix
dump(all_X, "all_X.lib")

##############################################################################
# Finetuning a pretrained CNN and extracting the second last layer as features
##############################################################################

# m ... number of samples used for finetuning
m = min(750,n)

# 80% training and 20% validation
n_train = round(m*0.8)
indices_train = random.sample(range(750),n_train)

input_train = input_list[:m,:][indices_train,:]
input_validate = np.delete((input_list[:m,:]),indices_train, axis = 0)

# Since the dataset is unbalanced, we use a weighted sampler 
class_counts = np.zeros(num_classes)
for i in range(num_classes): 
    class_counts[i] = sum(input_train[:,5]==i)
weight = 1/class_counts
sample_weights = np.array([weight[t] for t in input_train[:,5]])
sample_weights = torch.from_numpy(sample_weights)
sample_weights = sample_weights.double()
sampler = torch.utils.data.WeightedRandomSampler(weights=sample_weights, 
                                                 num_samples=len(sample_weights))

dataset = {'train' : mimicDataset(input_train),
           'val': mimicDataset(input_validate)}

dataloaders = {'train': DataLoader(dataset['train'] , batch_size=4, num_workers=0, sampler = sampler),
               'val': DataLoader(dataset['val'] , batch_size=4, num_workers=0 )}


dataset_sizes = {x: len(dataset[x]) for x in ['train', 'val']}

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
# https://pytorch.org/tutorials/beginner/transfer_learning_tutorial.html
def train_model(model, criterion, optimizer, scheduler, num_epochs=25):

    best_model_wts = copy.deepcopy(model.state_dict())
    best_acc = 0.0

    for epoch in range(num_epochs):
        print('Epoch {}/{}'.format(epoch, num_epochs - 1))
        print('-' * 10)

        # Each epoch has a training and validation phase
        for phase in ['train', 'val']:
            if phase == 'train':
                model.train()  # Set model to training mode
            else:
                model.eval()   # Set model to evaluate mode

            running_loss = 0.0
            running_corrects = 0

            # Iterate over data.
            for inputs, labels in dataloaders[phase]:
                inputs = inputs.to(device)
                labels = labels.to(device)

                # forward
                # track history if only in train
                with torch.set_grad_enabled(phase == 'train'):
                    outputs = model(inputs)
                    _, preds = torch.max(outputs, 1)
                    loss = criterion(outputs, labels)

                    # backward + optimize only if in training phase
                    if phase == 'train':
                        optimizer.zero_grad()
                        loss.backward()
                        optimizer.step()

                # statistics
                running_loss += loss.item() * inputs.size(0)
                running_corrects += torch.sum(preds == labels.data)

            if phase == 'train':
                scheduler.step()

            epoch_loss = running_loss / dataset_sizes[phase]
            epoch_acc = running_corrects.double() / dataset_sizes[phase]

            print('{} Loss: {:.4f} Acc: {:.4f}'.format(
                phase, epoch_loss, epoch_acc))

            # deep copy the model
            if phase == 'val' and epoch_acc > best_acc:
                best_acc = epoch_acc
                best_model_wts = copy.deepcopy(model.state_dict())

        print()

    print('Best val Acc: {:4f}'.format(best_acc), )

    # load best model weights
    model.load_state_dict(best_model_wts)
    return model


model = models.resnet50(pretrained=True)
num_ftrs = model.fc.in_features
# set output size to 14 (number of classes)
model.fc = nn.Linear(num_ftrs, num_classes)
model = model.to(device)
criterion = nn.CrossEntropyLoss()
optimizer = optim.Adam(model.parameters(), lr=0.0001)
step_lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=7, gamma=0.1)
model = train_model(model, criterion, optimizer, step_lr_scheduler, num_epochs=2)

modules = list(model.children())[:-1]
model=torch.nn.Sequential(*modules)
for p in model.parameters():
    p.requires_grad = False
    
model.eval()
# apply modified resnet50 to data
dataloaders = DataLoader(mimicDataset(input_list[:n,:]), batch_size=n,num_workers=0)
    
data, labels = next(iter(dataloaders))
with torch.no_grad():
    features_var = model(data)
    features = features_var.data 
    all_X_finetuned = features.reshape(n,2048).numpy()

# save features matrix
dump(all_X_finetuned, "all_X_finetuned.lib")


